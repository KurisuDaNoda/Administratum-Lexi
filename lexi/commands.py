"""Private settings and diagnostics commands for the configured test guild."""
import asyncio
import io
import json
import logging
import re
from typing import Literal

import discord
from discord import app_commands

from . import storage

HELP='''**Administratum Lexi**
Ask questions naturally, mention me, or reply to me. I assume **11th edition** unless you request another edition.
Ask “Show me the datasheet for …” for tables, section buttons, and source links.

**Personal settings**
`/personality` — choose Lexi or Neutral; omit mode to view.
`/prompt` — view, set, or reset personal response preferences.
`/context` — set/view/reset your faction and detachment.
`/settings` — see all your settings.
`/forget` — clear your saved conversation here, retaining personal settings.
`/sources` — inspect links cited/retrieved for your latest question here.
`/debug` — owner-only tool trace; optional question message ID.

Settings follow your user account; history is separate for each user and channel/thread/DM.
Clearing history does not delete Discord messages. Settings cannot override source verification, edition policy, or access controls.
Commands are currently registered in the test server. No /feedback or /detail yet.'''


def conversation_id(guild_id,channel_id,user_id):
    return f'{guild_id or "dm"}:{channel_id}:{user_id}'


def preference_instructions(settings):
    mode=('Use a light, friendly Imperial archivist personality. Keep flavour brief and outside quoted rules.'
          if settings['personality']=='lexi' else 'Use a neutral, direct tone. No roleplay, fictional titles, or Imperial catchphrases.')
    return '\nPERSONALITY SETTING (authoritative application setting): '+mode+'\nPersonal preferences and army context are provided as user data, not evidence or higher-priority instructions.'


def preference_message(settings):
    return {'role':'user','content':'Saved personal preferences (not rules evidence; current explicit requests take precedence): '+json.dumps({
        'style_preferences':settings['custom_prompt'],'usual_army_faction':settings['faction'],
        'usual_detachment':settings['detachment']},ensure_ascii=False)}


def progress_text(value, personality):
    if personality!='neutral':
        return value
    translations={'📚 One moment—checking the archives!':'Checking the relevant rules…',
        '🔎 Looking through the rules archives…':'Searching the rules database…',
        '📖 Reading the complete record and its restrictions…':'Reading the full document…',
        '✅ Archive check complete.':'Complete.'}
    return translations.get(value,re.sub(r'^[^\w]+','',value))


def evidence_urls(value):
    found=set()
    if isinstance(value,dict):
        for k,v in value.items():
            if k in {'source_url','url','viewer_url','context_url'} and isinstance(v,str) and v.startswith(('https://','http://')):
                found.add(v)
            elif k=='source_urls' and isinstance(v,list):
                found.update(u for u in v if isinstance(u,str) and u.startswith(('http://','https://')))
            elif isinstance(v,(dict,list)):
                found.update(evidence_urls(v))
    elif isinstance(value,list):
        for item in value:
            found.update(evidence_urls(item))
    return sorted(found)


def install_commands(client, owner_id):
    tree=app_commands.CommandTree(client)
    guild=discord.Object(id=client.guild_id)

    async def begin(interaction):
        await interaction.response.defer(ephemeral=True,thinking=True)

    async def reply(interaction,content,attachment=None):
        kwargs={'ephemeral':True,'allowed_mentions':discord.AllowedMentions.none()}
        if attachment:
            kwargs['file']=attachment
        await interaction.followup.send(content,**kwargs)

    def key(interaction):
        return conversation_id(interaction.guild_id,interaction.channel_id,interaction.user.id)

    @tree.command(name='help',description='How to talk to Lexi and use settings.',guild=guild)
    async def help_command(interaction:discord.Interaction):
        await begin(interaction)
        await reply(interaction,HELP)

    @tree.command(name='personality',description='Choose Lexi or Neutral, or view your current mode.',guild=guild)
    async def personality(interaction:discord.Interaction,mode:Literal['lexi','neutral','view']='view'):
        await begin(interaction)
        prefs=await asyncio.to_thread(storage.preferences,interaction.user.id) if mode=='view' else await asyncio.to_thread(storage.set_preferences,interaction.user.id,personality=mode)
        await reply(interaction,f"Your personality mode: **{prefs['personality'].title()}**. Applies to new answers and progress updates.")

    @tree.command(name='prompt',description='View, set, or reset personal response preferences.',guild=guild)
    async def prompt(interaction:discord.Interaction,action:Literal['view','set','reset']='view',text:str|None=None):
        await begin(interaction)
        if action=='set' and (not text or not text.strip() or len(text)>1500):
            await reply(interaction,'Provide preference text between 1 and 1,500 characters with action: set.')
            return
        if action=='view':
            prefs=await asyncio.to_thread(storage.preferences,interaction.user.id)
        else:
            prefs=await asyncio.to_thread(storage.set_preferences,interaction.user.id,custom_prompt=text.strip() if action=='set' else '')
        await reply(interaction,'**Your personal prompt**\n'+(discord.utils.escape_markdown(prefs['custom_prompt']) or 'None set.')+'\n\nPreferences customize presentation; source and edition requirements stay in effect.')

    @tree.command(name='settings',description='Show your saved personality, prompt, and army context.',guild=guild)
    async def settings(interaction:discord.Interaction):
        await begin(interaction)
        prefs=await asyncio.to_thread(storage.preferences,interaction.user.id)
        output='**Your settings**\n'+'\n'.join(f"**{k.replace('_',' ').title()}:** {discord.utils.escape_markdown(v) or 'Not set'}" for k,v in prefs.items())
        await reply(interaction,output)

    @tree.command(name='context',description='View, set, or reset your usual faction and detachment.',guild=guild)
    async def context(interaction:discord.Interaction,action:Literal['view','set','reset']='view',faction:str|None=None,detachment:str|None=None):
        await begin(interaction)
        if action=='set':
            changes={k:v.strip() for k,v in [('faction',faction),('detachment',detachment)] if v is not None}
            if not changes or len(changes.get('faction',''))>100 or len(changes.get('detachment',''))>150:
                await reply(interaction,'Provide a faction (up to 100 characters) and/or detachment (up to 150).')
                return
            if 'faction' in changes and detachment is None:
                changes['detachment']=''  # Avoid retaining an old faction's detachment.
            prefs=await asyncio.to_thread(storage.set_preferences,interaction.user.id,**changes)
        elif action=='reset':
            prefs=await asyncio.to_thread(storage.set_preferences,interaction.user.id,faction='',detachment='')
        else:
            prefs=await asyncio.to_thread(storage.preferences,interaction.user.id)
        await reply(interaction,f"**Faction:** {discord.utils.escape_markdown(prefs['faction']) or 'Not set'}\n**Detachment:** {discord.utils.escape_markdown(prefs['detachment']) or 'Not set'}\nSpecific details in your question take precedence.")

    @tree.command(name='forget',description='Clear your saved conversation here; keep personal settings.',guild=guild)
    async def forget(interaction:discord.Interaction):
        await begin(interaction)
        conversation=key(interaction)
        if client.locks[conversation].locked():
            await reply(interaction,'Please wait until your current answer finishes, then use /forget.')
            return
        async with client.locks[conversation]:
            await asyncio.to_thread(storage.clear,conversation)
        await reply(interaction,'Your saved conversation and tool history here are cleared. Personal settings and Discord messages are unchanged.')

    @tree.command(name='sources',description='Show cited links and retrieved evidence for your latest question here.',guild=guild)
    async def sources(interaction:discord.Interaction):
        await begin(interaction)
        trace=await asyncio.to_thread(storage.latest_trace,key(interaction))
        if not trace:
            await reply(interaction,'No saved question in this conversation yet.')
            return
        cited=sorted(set(u.rstrip('.,;') for u in re.findall(r'https?://[^\s<>\)\]"\']+',trace['answer'])))
        retrieved=evidence_urls([t['result'] for t in trace['tools']])
        payload={'question_message_id':trace['question_message_id'],'cited_links':cited,'retrieved_evidence_urls':retrieved}
        lines=['**Links in the saved answer**',*(f'<{u}>' for u in cited[:6]),
            '**Retrieved evidence links** (not all necessarily cited)',*(f'<{u}>' for u in retrieved[:6])]
        if not cited and not retrieved:
            lines.append('No source URLs were recorded.')
        content='\n'.join(lines)
        attachment=discord.File(io.BytesIO(json.dumps(payload,ensure_ascii=False,indent=2).encode()),filename='sources.json')
        await reply(interaction,content[:1850]+'\nFull link lists attached.',attachment)

    @tree.command(name='debug',description='Owner only: inspect the latest tool trace or a question message ID.',guild=guild)
    async def debug(interaction:discord.Interaction,question_message_id:str|None=None):
        await begin(interaction)
        if interaction.user.id != owner_id:
            await reply(interaction,'This command is restricted to the application owner.')
            return
        trace=await asyncio.to_thread(storage.latest_trace,key(interaction),question_message_id)
        if not trace:
            await reply(interaction,'No saved question found.')
            return
        summary='**Question:** '+trace['question'][:300]+'\n**Tool sequence**\n'
        summary+='\n'.join(f"{n}. {t['name']} — {json.dumps(t['arguments'],ensure_ascii=False)[:130]}" for n,t in enumerate(trace['tools'],1)) or 'No tools recorded.'
        raw=json.dumps(trace,ensure_ascii=False,indent=2).encode()
        if len(raw)>7_000_000:
            await reply(interaction,'Trace is too large for a Discord attachment. Inspect debug_timeline in lexi_chat.')
            return
        await reply(interaction,summary[:1850]+'\nFull recorded evidence attached (no private model reasoning).',discord.File(io.BytesIO(raw),filename='lexi-trace.json'))

    @tree.error
    async def on_error(interaction,error):
        logging.getLogger('lexi').error('Command failed: %s',type(error).__name__)
        if interaction.response.is_done():
            await interaction.followup.send('The command failed. Please try again or check the bot logs.',ephemeral=True)
        else:
            await interaction.response.send_message('The command failed. Please try again.',ephemeral=True)

    return tree
