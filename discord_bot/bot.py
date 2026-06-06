import os
import logging
import httpx
import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger(__name__)

AGENT_URL = os.getenv("AGENT_URL", "http://k8s-ai-agent.ai-agent.svc.cluster.local")
TOKEN = os.environ["DISCORD_BOT_TOKEN"]


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self):
        log.info("Bot ready: %s", self.user)


bot = Bot()


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    if interaction.data.get("component_type") != 2:  # button
        return

    custom_id: str = interaction.data.get("custom_id", "")
    await interaction.response.defer(ephemeral=True)

    async with httpx.AsyncClient(timeout=15) as client:
        if custom_id.startswith("approve:"):
            inv_id = custom_id[len("approve:"):]
            r = await client.post(f"{AGENT_URL}/investigations/{inv_id}/approve")
            if r.status_code == 200:
                await interaction.followup.send(f"✅ Approved `{inv_id}` — will be ingested into RAG.", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed ({r.status_code}): {r.text}", ephemeral=True)

        elif custom_id.startswith("reject:"):
            inv_id = custom_id[len("reject:"):]
            r = await client.post(f"{AGENT_URL}/investigations/{inv_id}/reject")
            if r.status_code == 200:
                await interaction.followup.send(f"❌ Rejected `{inv_id}`.", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed ({r.status_code}): {r.text}", ephemeral=True)

        elif custom_id.startswith("apply:"):
            inv_id = custom_id[len("apply:"):]
            r = await client.get(f"{AGENT_URL}/investigations/{inv_id}")
            if r.status_code != 200:
                await interaction.followup.send(f"Could not load investigation ({r.status_code}).", ephemeral=True)
                return
            inv = r.json()
            action = inv.get("proposed_action")
            if not action:
                await interaction.followup.send("No proposed_action stored in investigation.", ephemeral=True)
                return
            r2 = await client.post(f"{AGENT_URL}/apply", json=action)
            if r2.status_code == 200:
                result = r2.json()
                await interaction.followup.send(
                    f"⚡ Applied `{action.get('action')}`: {result.get('message', 'done')}",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(f"Apply failed ({r2.status_code}): {r2.text}", ephemeral=True)

        else:
            log.warning("Unknown button custom_id: %s", custom_id)


@bot.tree.command(name="investigate", description="Trigger AI investigation of a namespace")
@app_commands.describe(namespace="Namespace to investigate (default: chaos)")
async def investigate(interaction: discord.Interaction, namespace: str = "chaos"):
    await interaction.response.defer()

    alerts = ["KubePodCrashLooping", "KubeContainerWaiting", "KubePodNotReady"]
    triggered = 0

    async with httpx.AsyncClient(timeout=15) as client:
        for alertname in alerts:
            try:
                r = await client.post(f"{AGENT_URL}/investigate", json={
                    "alertname": alertname,
                    "namespace": namespace,
                    "pod": "",
                    "severity": "warning",
                    "summary": f"Discord manual trigger by {interaction.user} for {namespace}",
                })
                if r.status_code == 200:
                    triggered += 1
            except Exception as e:
                log.warning("Failed to trigger %s: %s", alertname, e)

    await interaction.followup.send(
        f"Triggered {triggered} investigation(s) for namespace `{namespace}`. "
        f"Results will appear in the alerts channel."
    )


@bot.tree.command(name="investigations", description="List unreviewed investigations")
async def investigations(interaction: discord.Interaction):
    await interaction.response.defer()

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{AGENT_URL}/investigations")

    data = r.json()
    pending = [i for i in data["investigations"] if not i.get("reviewed")]

    if not pending:
        await interaction.followup.send("No pending investigations.")
        return

    embed = discord.Embed(title=f"Pending Investigations ({len(pending)})", color=0xf59e0b)
    for inv in pending[:10]:
        preview = (inv.get("diagnosis_preview") or "")[:120]
        embed.add_field(
            name=f"{inv['alert_name']} — {inv['namespace']}",
            value=f"`{inv['id']}`\n{preview}",
            inline=False,
        )
    embed.set_footer(text="Use /approve or /reject with the ID above")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="approve", description="Approve an investigation (adds to RAG learning)")
@app_commands.describe(investigation_id="Investigation filename from /investigations")
async def approve(interaction: discord.Interaction, investigation_id: str):
    await interaction.response.defer()

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{AGENT_URL}/investigations/{investigation_id}/approve")

    if r.status_code == 200:
        await interaction.followup.send(f"Approved `{investigation_id}` — will be ingested into RAG.")
    else:
        await interaction.followup.send(f"Failed ({r.status_code}): {r.text}")


@bot.tree.command(name="reject", description="Reject an investigation")
@app_commands.describe(investigation_id="Investigation filename from /investigations")
async def reject(interaction: discord.Interaction, investigation_id: str):
    await interaction.response.defer()

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{AGENT_URL}/investigations/{investigation_id}/reject")

    if r.status_code == 200:
        await interaction.followup.send(f"Rejected `{investigation_id}`.")
    else:
        await interaction.followup.send(f"Failed ({r.status_code}): {r.text}")


bot.run(TOKEN)
