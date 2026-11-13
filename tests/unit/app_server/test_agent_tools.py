import pytest
from pydantic import BaseModel

from openhands.app_server.settings.agent_tools import (
    effective_agent_tools,
    with_managed_subagents,
)
from openhands.sdk.profiles import OpenHandsAgentProfile, build_seed_profile
from openhands.sdk.settings import OpenHandsAgentSettings
from openhands.sdk.tool import SUB_AGENT_TOOL_NAME, Tool


def _settings_with_subagents(enabled: bool) -> OpenHandsAgentSettings:
    if 'enable_sub_agents' in OpenHandsAgentSettings.model_fields:
        return OpenHandsAgentSettings(enable_sub_agents=enabled)
    tools = [Tool(name=SUB_AGENT_TOOL_NAME)] if enabled else []
    return OpenHandsAgentSettings(tools=tools)


@pytest.mark.parametrize('enabled', [True, False])
def test_effective_agent_tools_follow_installed_sdk_contract(enabled: bool):
    settings = _settings_with_subagents(enabled)

    effective = effective_agent_tools(settings)

    assert effective.sub_agents_enabled is enabled
    if 'enable_sub_agents' in OpenHandsAgentSettings.model_fields:
        assert effective.use_default_tools is True
        assert effective.tools is None
    else:
        assert effective.use_default_tools is False
        assert effective.tools is not None
        assert (
            any(tool.name == SUB_AGENT_TOOL_NAME for tool in effective.tools) is enabled
        )


def test_managed_subagents_converge_to_installed_sdk_contract():
    settings = _settings_with_subagents(False)

    converged = with_managed_subagents(settings, enable_browser=True)
    effective = effective_agent_tools(converged)

    assert effective.sub_agents_enabled is True

    profile = build_seed_profile(converged, active_llm_profile=None)
    assert isinstance(profile, OpenHandsAgentProfile)
    assert any(tool.name == SUB_AGENT_TOOL_NAME for tool in profile.tools or [])


def test_partial_retired_tool_schema_is_rejected():
    class PartialLegacySettings(BaseModel):
        tools: list[Tool] | None = None
        enable_sub_agents: bool = True

    with pytest.raises(ValueError, match='both retired tool switches'):
        effective_agent_tools(PartialLegacySettings())  # type: ignore[arg-type]
