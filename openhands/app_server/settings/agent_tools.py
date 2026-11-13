from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

from openhands.sdk.settings import OpenHandsAgentSettings
from openhands.sdk.tool import SUB_AGENT_TOOL_NAME, Tool, default_tool_specs

_AgentSettingsT = TypeVar('_AgentSettingsT', bound=OpenHandsAgentSettings)
_RETIRED_TOOL_SWITCHES = {'enable_sub_agents', 'enable_switch_llm_tool'}


class _CurrentAgentToolSettings(BaseModel):
    model_config = ConfigDict(extra='ignore')

    tools: list[Tool] | None


class _LegacyAgentToolSettings(_CurrentAgentToolSettings):
    enable_sub_agents: bool
    enable_switch_llm_tool: bool


@dataclass(frozen=True)
class EffectiveAgentTools:
    tools: list[Tool] | None
    use_default_tools: bool
    sub_agents_enabled: bool


def _parse_agent_tool_settings(
    settings: OpenHandsAgentSettings,
) -> _CurrentAgentToolSettings | _LegacyAgentToolSettings:
    data = settings.model_dump(mode='python')
    present_retired_switches = _RETIRED_TOOL_SWITCHES.intersection(data)
    if present_retired_switches == _RETIRED_TOOL_SWITCHES:
        return _LegacyAgentToolSettings.model_validate(data)
    if not present_retired_switches:
        return _CurrentAgentToolSettings.model_validate(data)
    raise ValueError(
        'OpenHands agent settings must define both retired tool switches, or neither'
    )


def effective_agent_tools(
    settings: OpenHandsAgentSettings,
) -> EffectiveAgentTools:
    source = _parse_agent_tool_settings(settings)
    if isinstance(source, _LegacyAgentToolSettings):
        return EffectiveAgentTools(
            tools=None,
            use_default_tools=True,
            sub_agents_enabled=source.enable_sub_agents,
        )
    if source.tools is None:
        return EffectiveAgentTools(
            tools=None,
            use_default_tools=True,
            sub_agents_enabled=False,
        )
    return EffectiveAgentTools(
        tools=list(source.tools),
        use_default_tools=False,
        sub_agents_enabled=any(
            tool.name == SUB_AGENT_TOOL_NAME for tool in source.tools
        ),
    )


def with_managed_subagents(
    settings: _AgentSettingsT,
    *,
    enable_browser: bool,
) -> _AgentSettingsT:
    source = _parse_agent_tool_settings(settings)
    if isinstance(source, _LegacyAgentToolSettings):
        tools = (
            list(source.tools)
            if source.tools is not None
            else default_tool_specs(enable_browser=enable_browser)
        )
        if not any(tool.name == SUB_AGENT_TOOL_NAME for tool in tools):
            tools.append(Tool(name=SUB_AGENT_TOOL_NAME))
        updates: dict[str, object] = {
            'enable_sub_agents': True,
            'tools': tools,
        }
    else:
        tools = (
            list(source.tools)
            if source.tools is not None
            else default_tool_specs(enable_browser=enable_browser)
        )
        if not any(tool.name == SUB_AGENT_TOOL_NAME for tool in tools):
            tools.append(Tool(name=SUB_AGENT_TOOL_NAME))
        updates = {'tools': tools}
    return settings.model_copy(update=updates)
