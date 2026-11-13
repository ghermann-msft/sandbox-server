from collections.abc import Callable
from typing import Any, Literal

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openhands.agent_server.env_parser import from_env
from openhands.sdk.llm import LLM
from pydantic import BaseModel, Field, PrivateAttr, SecretStr

AZURE_OPENAI_SCOPE = 'https://cognitiveservices.azure.com/.default'


class ServerManagedLLMEnvironment(BaseModel):
    model: str
    api_key: SecretStr | None = None
    auth_type: Literal['api_key', 'subscription', 'azure_workload_identity'] = 'api_key'
    base_url: str | None = None
    api_version: str | None = None


class ServerManagedLLM(LLM):
    """Deployment-managed LLM configuration for app-server launches."""

    auth_type: Literal['api_key', 'subscription', 'azure_workload_identity'] = Field(
        default='api_key', description='Authentication mode for the LLM.'
    )
    _azure_ad_token_provider: Callable[[], str] | None = PrivateAttr(default=None)

    def _azure_workload_identity_kwargs(self) -> dict[str, Callable[[], str]]:
        if self.auth_type != 'azure_workload_identity':
            return {}
        if self._azure_ad_token_provider is None:
            credential = DefaultAzureCredential()
            self._azure_ad_token_provider = get_bearer_token_provider(
                credential, AZURE_OPENAI_SCOPE
            )
        return {'azure_ad_token_provider': self._azure_ad_token_provider}

    def _prepare_transport_kwargs(
        self,
        *,
        messages: list[dict[str, Any]],
        enable_streaming: bool,
        auth_values: tuple[str | None, dict[str, str]] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        result = super()._prepare_transport_kwargs(
            messages=messages,
            enable_streaming=enable_streaming,
            auth_values=auth_values,
            **kwargs,
        )
        azure_auth_kwargs = self._azure_workload_identity_kwargs()
        if azure_auth_kwargs:
            result.pop('api_key', None)
            result.pop('azure_ad_token', None)
            result.update(azure_auth_kwargs)
        return result


def load_server_managed_llm(
    allow_user_llm_configuration: bool,
) -> ServerManagedLLM | None:
    if allow_user_llm_configuration:
        return None
    environment = from_env(ServerManagedLLMEnvironment, 'LLM')
    return ServerManagedLLM(**environment.model_dump(context={'expose_secrets': True}))
