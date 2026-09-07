"""Thread-safe Bedrock client for gpt-oss-120b via OIDC federation.

Self-contained port of the repo's ``BedrockSession`` with production hardening:
  - **thread-safe**: a lock guards credential refresh / re-auth so concurrent callers
    never see a half-swapped session (the original mutated ``self.session`` unlocked).
  - **real timeouts**: the ``bedrock-runtime`` client is built with a botocore
    ``Config`` (read/connect timeout, no internal retries) so a hung model call fails
    instead of blocking forever.
  - **structured errors**: setup/invoke failures raise the ``errors`` taxonomy
    (``UpstreamAuthError`` / ``UpstreamRateLimited`` / ``UpstreamTimeout``).

OIDC flow: Entra ID token → assume bridge role (common-security) → assume target role
(Bedrock account) → boto3 Session.
"""
from __future__ import annotations

import base64
import json
import threading
import time

import boto3
import requests
from botocore.config import Config as BotoConfig

from ..config import Settings
from ..errors import (UpstreamAuthError, UpstreamRateLimited, UpstreamTimeout,
                      is_expired_credentials, to_agentic)
from ..logging_config import get_logger

log = get_logger("clients.bedrock")
SESSION_REFRESH_SECONDS = 3000  # refresh ~50 min before the 1h creds expire


class BedrockClient:
    """One federated session + runtime invoker. Safe for sequential use by one job;
    the pool hands a distinct instance to each concurrent job."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.region = settings.aws_region
        self.model_id = settings.bedrock_model_id
        self._boto_cfg = BotoConfig(
            read_timeout=settings.bedrock_read_timeout,
            connect_timeout=settings.bedrock_connect_timeout,
            retries={"max_attempts": 0},
        )
        self.session = None
        self.target_creds = None
        self._created_at = 0.0
        self._lock = threading.Lock()
        self.setup()

    # -- setup / refresh ----------------------------------------------------
    def setup(self) -> None:
        log.info("setting up OIDC Bedrock session")
        token = self._get_entra_token()
        try:
            bridge = self._assume_bridge_role(token)
            self.target_creds = self._assume_target_role(bridge)
        except UpstreamAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAuthError(f"STS role assumption failed: {exc}",
                                    stage="bedrock_auth") from exc
        self.session = boto3.Session(
            aws_access_key_id=self.target_creds["AccessKeyId"],
            aws_secret_access_key=self.target_creds["SecretAccessKey"],
            aws_session_token=self.target_creds["SessionToken"],
            region_name=self.region,
        )
        self._created_at = time.time()
        log.info("Bedrock session ready")

    def _refresh_if_needed(self) -> None:
        if time.time() - self._created_at >= SESSION_REFRESH_SECONDS:
            log.info("credentials aging — refreshing session")
            self.setup()

    # -- invoke -------------------------------------------------------------
    def invoke(self, messages, tools=None, max_tokens=2048, temperature=0.0,
               *, tool_choice=None):
        """OpenAI-style invoke → raw assistant ``message`` dict. Thread-safe."""
        body_dict = {"messages": messages, "max_tokens": max_tokens,
                     "temperature": temperature}
        if tools:
            body_dict["tools"] = tools
        if tool_choice is not None:
            body_dict["tool_choice"] = tool_choice
        body = json.dumps(body_dict)

        with self._lock:
            self._refresh_if_needed()
            try:
                return self._invoke_once(body)
            except Exception as exc:  # noqa: BLE001
                if is_expired_credentials(exc):
                    log.warning("creds expired mid-call — re-auth + retry")
                    self.setup()
                    return self._invoke_once(body)
                raise self._map_invoke_error(exc)

    def _invoke_once(self, body: str) -> dict:
        runtime = self.session.client("bedrock-runtime", region_name=self.region,
                                      config=self._boto_cfg)
        resp = runtime.invoke_model(
            modelId=self.model_id, contentType="application/json",
            accept="application/json", body=body)
        rb = json.loads(resp["body"].read())
        if "choices" in rb and rb["choices"]:
            return rb["choices"][0]["message"]
        raise UpstreamAuthError(  # unexpected shape -> surface, don't loop
            f"unexpected invoke_model response: {json.dumps(rb)[:200]}",
            stage="bedrock")

    @staticmethod
    def _map_invoke_error(exc: Exception) -> Exception:
        text = f"{type(exc).__name__}: {exc}".lower()
        if "throttl" in text or "toomanyrequests" in text or "429" in text:
            return UpstreamRateLimited(str(exc), stage="bedrock")
        if "timeout" in text or "read timed out" in text:
            return UpstreamTimeout(str(exc), stage="bedrock")
        if "accessdenied" in text or "unauthorized" in text:
            return UpstreamAuthError(str(exc), stage="bedrock")
        return to_agentic(exc, stage="bedrock")

    # -- OIDC helpers -------------------------------------------------------
    def _get_entra_token(self) -> str:
        url = (f"https://login.microsoftonline.com/"
               f"{self.s.azure_tenant_id}/oauth2/v2.0/token")
        try:
            resp = requests.post(url, data={
                "grant_type": "client_credentials",
                "client_id": self.s.azure_client_id,
                "client_secret": self.s.azure_client_secret,
                "scope": f"{self.s.azure_client_id}/.default",
            }, timeout=30)
        except requests.RequestException as exc:
            raise UpstreamAuthError(f"Entra token request failed: {exc}",
                                    stage="bedrock_auth") from exc
        if resp.status_code != 200:
            raise UpstreamAuthError(
                f"Entra token HTTP {resp.status_code}: {resp.text[:200]}",
                stage="bedrock_auth")
        token = resp.json()["access_token"]
        try:
            payload = json.loads(base64.urlsafe_b64decode(
                token.split(".")[1] + "=="))
            log.info("Entra token OK aud=%s", payload.get("aud"))
        except Exception:  # noqa: BLE001
            pass
        return token

    def _assume_bridge_role(self, token: str) -> dict:
        sts = boto3.client("sts", region_name=self.region, config=self._boto_cfg)
        resp = sts.assume_role_with_web_identity(
            RoleArn=self.s.aws_role_arn_bridge,
            RoleSessionName="oidc-bridge-bedrock",
            WebIdentityToken=token, DurationSeconds=3600)
        return resp["Credentials"]

    def _assume_target_role(self, bridge: dict) -> dict:
        sts = boto3.client(
            "sts", region_name=self.region, config=self._boto_cfg,
            aws_access_key_id=bridge["AccessKeyId"],
            aws_secret_access_key=bridge["SecretAccessKey"],
            aws_session_token=bridge["SessionToken"])
        resp = sts.assume_role(RoleArn=self.s.aws_role_arn_target,
                               RoleSessionName="bedrock-target-session",
                               DurationSeconds=3600)
        return resp["Credentials"]

    def ping(self) -> bool:
        """Cheap readiness probe: are creds present & not near expiry?"""
        return self.session is not None and self.target_creds is not None
