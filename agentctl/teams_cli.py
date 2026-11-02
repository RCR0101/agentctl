"""Human team controls; coordinator tools use the separate scoped MCP interface."""

import json
from typing import Annotated, Any

import typer

app = typer.Typer(invoke_without_command=True, help="Connect coordinator and worker sessions.")
Permission = Annotated[str, typer.Option(help="observe, propose (default), or delegate")]


def request(method: str, **params: Any) -> Any:
    from agentctl.cli import rpc

    return rpc(method, False, **params)


def show(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


@app.callback()
def teams(ctx: typer.Context) -> None:
    """Open the interactive team graph, or use a subcommand."""
    if ctx.invoked_subcommand is None:
        from agentctl.tui import Watch

        request("ping")
        Watch(start_teams=True).run()


@app.command("ls")
def listing() -> None:
    show(request("teams_list"))


@app.command()
def create(
    name: str,
    coordinator: str,
    goal: str,
    permission_ceiling: Annotated[
        str, typer.Option(help="Maximum worker grant: observe, propose, or delegate")
    ] = "propose",
) -> None:
    """Save a team. Does not send a message or start routing."""
    show(
        request(
            "team_create",
            name=name,
            coordinator=coordinator,
            goal=goal,
            permission_ceiling=permission_ceiling,
        )
    )


@app.command("show")
def detail(team: str) -> None:
    show(request("team_get", team_id=team))


@app.command()
def add(
    team: str,
    session: str,
    role: str,
    permission: Permission = "propose",
    brief: str = "",
    model: str = "",
) -> None:
    """Connect an existing worker session. Use the graph to edit or rebind it."""
    show(
        request(
            "team_add_worker",
            team_id=team,
            session_id=session,
            role=role,
            permission=permission,
            brief=brief,
            model=model,
        )
    )


@app.command()
def setup(team: str, rotate: bool = False) -> None:
    """Get private scoped MCP configuration for the coordinator."""
    show(request("team_mcp_setup", team_id=team, rotate=rotate))


@app.command()
def start(team: str) -> None:
    """Enable assignment routing, after scoped MCP connects. Does not send the goal."""
    show(request("team_start", team_id=team))


@app.command()
def pause(team: str) -> None:
    """Stop future sends. Running agents continue."""
    show(request("team_pause", team_id=team))


@app.command()
def resume(team: str) -> None:
    show(request("team_resume", team_id=team))


@app.command()
def end(team: str) -> None:
    show(request("team_end", team_id=team))


@app.command()
def brief(team: str) -> None:
    """Print the coordinator briefing without sending it."""
    typer.echo(request("team_brief", team_id=team)["text"])


@app.command()
def assign(
    team: str, member: str, text: str, request_id: str | None = None, model: str | None = None
) -> None:
    """Explicit human assignment, queued until the worker is free."""
    params = {"model": model} if model is not None else {}
    show(
        request(
            "team_assign", team_id=team, member=member, text=text, request_id=request_id, **params
        )
    )


@app.command()
def review(team: str, assignment: str, action: str) -> None:
    """approve, accept, cancel, or resolve_unknown after inspecting delivery."""
    show(request("team_assignment_review", team_id=team, assignment=assignment, action=action))


@app.command()
def takeover(team: str, member: str) -> None:
    show(request("team_takeover", team_id=team, member=member))


@app.command()
def release(team: str, member: str) -> None:
    """Allow future team assignments after human takeover."""
    show(request("team_release", team_id=team, member=member))
