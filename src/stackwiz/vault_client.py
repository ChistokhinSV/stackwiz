"""Vault client: init/unseal + KV v2 + per-service policies via hvac."""
from __future__ import annotations

import json
import logging
import os
import warnings
from dataclasses import dataclass
from pathlib import Path

import hvac
import urllib3

log = logging.getLogger("stackwiz.vault")

KV_MOUNT = "stackwiz"
UNSEAL_SHARES = 5
UNSEAL_THRESHOLD = 3


def resolve_verify(explicit: bool | str | None = None) -> bool | str:
    """Resolve the TLS-verify setting for requests to Vault.

    Precedence (first match wins):
      1. `explicit` arg (tests / callers that already know)
      2. `VAULT_CACERT` env — path to a CA bundle (returned as the path)
      3. `STACKWIZ_VAULT_VERIFY=false|0|no` — explicit opt-out (returns False)
      4. Default: True (system CA trust)

    A False result is logged at WARNING the first time we return one per
    process, so a misconfiguration doesn't slip through silently.
    """
    if explicit is not None:
        return explicit
    cacert = os.environ.get("VAULT_CACERT", "").strip()
    if cacert:
        return cacert
    raw = os.environ.get("STACKWIZ_VAULT_VERIFY", "").strip().lower()
    if raw in {"false", "0", "no"}:
        if not getattr(resolve_verify, "_warned", False):
            log.warning(
                "TLS verification against Vault is DISABLED "
                "(STACKWIZ_VAULT_VERIFY=%s). Set VAULT_CACERT to a trust "
                "bundle to re-enable.", raw,
            )
            resolve_verify._warned = True  # type: ignore[attr-defined]
        return False
    return True


# When verification is off we still want the urllib3 warning suppressed so
# operator logs aren't swamped. Called by callers that set verify=False.
def suppress_insecure_warnings() -> None:
    warnings.filterwarnings(
        "ignore", category=urllib3.exceptions.InsecureRequestWarning
    )


@dataclass
class VaultInit:
    root_token: str
    unseal_keys: list[str]


class VaultClient:
    """Thin facade around hvac for the operations stackwiz needs."""

    def __init__(
        self,
        address: str,
        token: str | None = None,
        verify: bool | str | None = None,
    ) -> None:
        self.address = address.rstrip("/")
        self._token = token or os.environ.get("VAULT_TOKEN", "") or None
        self._verify = resolve_verify(verify)
        if self._verify is False:
            suppress_insecure_warnings()
        self._client = hvac.Client(
            url=self.address,
            token=self._token,
            verify=self._verify,
        )

    # --- health / auth ----------------------------------------------------------

    def is_initialized(self) -> bool:
        return bool(self._client.sys.is_initialized())

    def is_sealed(self) -> bool:
        return bool(self._client.sys.is_sealed())

    def is_authenticated(self) -> bool:
        if not self._token:
            return False
        try:
            return bool(self._client.is_authenticated())
        except Exception:  # noqa: BLE001
            return False

    def set_token(self, token: str) -> None:
        self._token = token
        self._client.token = token

    @property
    def token(self) -> str | None:
        return self._token

    # --- init / unseal ----------------------------------------------------------

    def initialize(
        self,
        state_dir: Path,
        shares: int = UNSEAL_SHARES,
        threshold: int = UNSEAL_THRESHOLD,
    ) -> VaultInit:
        """Initialize a fresh Vault and persist unseal keys to state_dir.

        Writes `<state_dir>/vault-init.json` with mode 0600. The operator MUST
        back this file up and then delete it — the installer prints a loud
        warning to that effect.
        """
        result = self._client.sys.initialize(secret_shares=shares, secret_threshold=threshold)
        init = VaultInit(
            root_token=result["root_token"],
            unseal_keys=list(result["keys_base64"]),
        )
        target = Path(state_dir) / "vault-init.json"
        target.write_text(
            json.dumps(
                {"root_token": init.root_token, "unseal_keys": init.unseal_keys},
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        self.set_token(init.root_token)
        return init

    def unseal(self, keys: list[str]) -> None:
        for key in keys:
            self._client.sys.submit_unseal_key(key)
            if not self._client.sys.is_sealed():
                return

    # --- KV v2 mount bootstrap --------------------------------------------------

    def ensure_kv_mount(self, mount: str = KV_MOUNT) -> None:
        mounts = self._client.sys.list_mounted_secrets_engines()
        existing = mounts.get("data", mounts)
        if f"{mount}/" in existing:
            return
        self._client.sys.enable_secrets_engine(
            backend_type="kv",
            path=mount,
            options={"version": "2"},
        )

    # --- KV v2 put/get/delete ---------------------------------------------------

    def kv_put(self, path: str, data: dict[str, str], mount: str = KV_MOUNT) -> None:
        self._client.secrets.kv.v2.create_or_update_secret(
            path=path, secret=data, mount_point=mount
        )

    def kv_get(self, path: str, mount: str = KV_MOUNT) -> dict[str, str] | None:
        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=mount, raise_on_deleted_version=False
            )
        except hvac.exceptions.InvalidPath:
            return None
        except hvac.exceptions.VaultError:
            return None
        return dict(response["data"]["data"])

    def kv_delete_metadata(self, path: str, mount: str = KV_MOUNT) -> None:
        try:
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=path, mount_point=mount
            )
        except hvac.exceptions.VaultError:
            pass

    # --- per-service policies ---------------------------------------------------

    def apply_service_policy(
        self,
        service_prefix: str,
        component_id: str,
        mount: str = KV_MOUNT,
    ) -> str:
        name = f"{service_prefix}-{component_id}"
        hcl = (
            f'path "{mount}/data/{service_prefix}/{component_id}/*" '
            f'{{ capabilities = ["read"] }}\n'
            f'path "{mount}/metadata/{service_prefix}/{component_id}/*" '
            f'{{ capabilities = ["list"] }}\n'
        )
        self._client.sys.create_or_update_policy(name=name, policy=hcl)
        return name

    def revoke_service_policy(self, service_prefix: str, component_id: str) -> None:
        name = f"{service_prefix}-{component_id}"
        try:
            self._client.sys.delete_policy(name=name)
        except hvac.exceptions.VaultError:
            pass

    def revoke_install_policy(self, service_prefix: str, component_id: str) -> None:
        name = f"{service_prefix}-{component_id}-install"
        try:
            self._client.sys.delete_policy(name=name)
        except hvac.exceptions.VaultError:
            pass

    # --- install-time scoped tokens --------------------------------------------

    def create_install_policy(
        self,
        service_prefix: str,
        component_id: str,
        mount: str = KV_MOUNT,
    ) -> str:
        """Policy for install-time access.

        Broader than the runtime policy (apply_service_policy) — the install
        script needs to WRITE generated secrets under its own path and READ
        shared secrets. But it still can't touch other components' paths,
        mount other engines, or create policies.
        """
        name = f"{service_prefix}-{component_id}-install"
        hcl = (
            f'path "{mount}/data/{service_prefix}/{component_id}/*" '
            f'{{ capabilities = ["create", "update", "read", "delete", "list"] }}\n'
            f'path "{mount}/metadata/{service_prefix}/{component_id}/*" '
            f'{{ capabilities = ["list", "read", "delete"] }}\n'
            f'path "{mount}/data/{service_prefix}/shared/*" '
            f'{{ capabilities = ["read"] }}\n'
            f'path "{mount}/metadata/{service_prefix}/shared/*" '
            f'{{ capabilities = ["list", "read"] }}\n'
        )
        self._client.sys.create_or_update_policy(name=name, policy=hcl)
        return name

    def create_child_token(
        self,
        policies: list[str],
        ttl: str = "2h",
        display_name: str = "stackwiz-install",
    ) -> str | None:
        """Mint a non-renewable, non-orphan child token with narrow policies.

        Returns the token string, or None if minting fails (caller should warn
        and fall back to its own token — usually root — with a clear log line).
        """
        try:
            result = self._client.auth.token.create(
                policies=policies,
                ttl=ttl,
                renewable=False,
                no_parent=False,
                display_name=display_name,
            )
        except hvac.exceptions.VaultError:
            return None
        auth = result.get("auth") or {}
        token = auth.get("client_token")
        return token if isinstance(token, str) and token else None

    def revoke_token(self, token: str) -> None:
        """Best-effort revoke of a token. Silent on failure — the token's TTL
        is the real safety net."""
        if not token:
            return
        try:
            self._client.auth.token.revoke(token=token)
        except hvac.exceptions.VaultError:
            pass
