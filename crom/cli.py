"""CLI commands for crom"""

import click

from . import chrome, profiles


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """crom — Chrome profile manager"""
    profiles.ensure_default()
    chrome.copy_profile("default")
    if ctx.invoked_subcommand is None:
        ctx.invoke(up_cmd, name="default")


@main.command("list")
def list_cmd():
    """List all profiles"""
    for name in profiles.list_profiles():
        status = f"running, port={chrome.debug_port(name)}" if chrome.is_running(name) else "stopped"
        click.echo(f"  {name:20s}  [{status}]")


@main.command("add")
@click.argument("name", required=False)
def add_cmd(name: str | None):
    """Add a new profile (copies default Chrome profile)"""
    if not name:
        name = click.prompt("Profile name").strip()
    if not name:
        click.echo("Name cannot be empty.", err=True)
        return

    try:
        profiles.add_profile(name)
    except ValueError as e:
        click.echo(str(e), err=True)
        return

    click.echo(f"Copying Chrome profile to {profiles.profile_state_dir(name)} ...")
    chrome.copy_profile(name)
    click.echo(f"Created profile '{name}'")


@main.command("rm")
@click.argument("name")
def rm_cmd(name: str):
    """Remove a profile"""
    if chrome.is_running(name):
        click.echo(f"Profile '{name}' is running. Run: crom down {name}", err=True)
        return
    import shutil

    try:
        profiles.remove_profile(name)
    except KeyError as e:
        click.echo(str(e), err=True)
        return

    state_dir = profiles.profile_state_dir(name)
    if state_dir.exists():
        shutil.rmtree(state_dir)

    click.echo(f"Removed profile '{name}'")


@main.command("up")
@click.argument("name", required=False, default="default")
def up_cmd(name: str):
    """Launch a profile (idempotent — always reports the live CDP port)"""
    if profiles.get_profile(name) is None:
        click.echo(f"Unknown profile '{name}'. Run: crom list", err=True)
        return
    if chrome.is_running(name):
        click.echo(f"Profile '{name}' is already running on port {chrome.debug_port(name)}")
        return
    port = chrome.launch(name)
    click.echo(f"Started '{name}' on port {port}")


@main.command("down")
@click.argument("name", required=False, default="default")
def down_cmd(name: str):
    """Kill a running profile"""
    cfg = profiles.get_profile(name)
    if cfg is None:
        click.echo(f"Unknown profile '{name}'. Run: crom list", err=True)
        return
    if not chrome.is_running(name):
        click.echo(f"Profile '{name}' is not running")
        return
    pid = chrome.kill(name)
    click.echo(f"Stopped '{name}' (pid={pid})")
