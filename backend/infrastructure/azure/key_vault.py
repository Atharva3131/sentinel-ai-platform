"""Azure Key Vault secret access."""

from __future__ import annotations

from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

AzureCredential = DefaultAzureCredential | ClientSecretCredential

class AzureKeyVault:
    """Async adapter for Azure Key Vault."""

    def __init__(
        self,
        vault_url: str,
        credential: AzureCredential,
    ) -> None:
        self._credential = credential
        self._client = SecretClient(
            vault_url=vault_url,
            credential=credential,
        )

    async def get_secret(self, name: str) -> str:
        """Retrieve a secret value."""
        secret = await self._client.get_secret(name)
        if secret.value is None:
            raise ValueError(f"Key Vault secret has no value: {name}")
        return secret.value

    async def close(self) -> None:
        """Close the Key Vault client."""
        await self._client.close()
