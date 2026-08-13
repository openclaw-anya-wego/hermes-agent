"""Register the ACP delegation tool with Hermes.

Deliberately thin. Everything the tool does lives in the modules beside it, so
this file only answers "what is registered, and under what name".
"""

from plugins.acp_delegation.tools import (
    ACP_DELEGATE_SCHEMA,
    acpx_is_available,
    handle_acp_delegate,
)

# Not "delegation": that toolset already belongs to the built-in delegate_task,
# and registering across an existing toolset is rejected.
TOOLSET = "acp_delegation"


def register(ctx) -> None:
    ctx.register_tool(
        name="acp_delegate",
        toolset=TOOLSET,
        schema=ACP_DELEGATE_SCHEMA,
        handler=handle_acp_delegate,
        check_fn=acpx_is_available,
        emoji="🛠️",
    )
