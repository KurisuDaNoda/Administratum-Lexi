"""Run with python -m lexi.bot; --check initializes storage without connecting to Discord."""
import argparse
import asyncio
from collections import defaultdict
import logging
import os

import discord
from discord.ext import tasks

from scripts.import_data import ROOT, load_env
from . import storage
from .assistant import Assistant
from .datasheets import DatasheetDisplay, DatasheetView
from . import documents
from .commands import install_commands, conversation_id, progress_text

LOG = logging.getLogger('lexi')


def should_respond(message, bot_id, guild_id, channel_id):
    if message.author.bot or not message.content.strip():
        return False
    if message.guild is None:
        return True
    if message.guild.id != guild_id:
        return False
    parent_id = getattr(message.channel, 'parent_id', None)
    if message.channel.id == channel_id or parent_id == channel_id:
        return True
    if any(user.id == bot_id for user in message.mentions):
        return True
    reference = message.reference
    return bool(reference and isinstance(reference.resolved, discord.Message) and reference.resolved.author.id == bot_id)


def split_message(text, maximum=1900):
    pieces = []
    while len(text) > maximum:
        position = text.rfind('\n', 0, maximum)
        if position < maximum // 2:
            position = maximum
        pieces.append(text[:position])
        text = text[position:].lstrip('\n')
    if text:
        pieces.append(text)
    return pieces


class Lexi(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.assistant = Assistant()
        self.guild_id = int(os.environ['DISCORD_GUILD_ID'])
        self.channel_id = int(os.environ['DISCORD_CHANNEL_ID'])
        self.locks = defaultdict(asyncio.Lock)
        self.slots = asyncio.Semaphore(3)

    async def setup_hook(self):
        app = await self.application_info()
        owner_id = app.team.owner_id if app.team else app.owner.id
        self.command_tree = install_commands(self, owner_id)
        registered = await self.command_tree.sync(guild=discord.Object(id=self.guild_id))
        LOG.info('Registered %s slash commands in the test server.',len(registered))
        self.cleanup.start()

    async def close(self):
        self.cleanup.cancel()
        await self.assistant.client.close()
        await super().close()

    @tasks.loop(hours=24)
    async def cleanup(self):
        await asyncio.to_thread(storage.prune)

    async def on_ready(self):
        LOG.info('Lexi connected. Configured test server/channel restrictions are active.')

    async def on_message(self, message):
        if not should_respond(message, self.user.id, self.guild_id, self.channel_id):
            return
        # Per-user/channel context prevents another member's messages entering a DM
        # or an unrelated user's ongoing discussion. Threads have distinct channel IDs.
        conversation = conversation_id(message.guild.id if message.guild else None,message.channel.id,message.author.id)
        if self.locks[conversation].locked():
            await message.reply('Still checking your previous question—please give me a moment.', mention_author=False)
            return
        async with self.locks[conversation], self.slots:
            if message.content.strip().lower() in {'forget this conversation', 'clear our conversation'}:
                await asyncio.to_thread(storage.clear, conversation)
                await message.reply('I’ve cleared our saved conversation here. Messages on Discord are unchanged.', mention_author=False)
                return
            await asyncio.to_thread(storage.prune)
            content = message.content
            for mention in (f'<@{self.user.id}>', f'<@!{self.user.id}>'):
                content = content.replace(mention, '').strip()
            content = f'{message.author.display_name}: {content}'
            saved = await asyncio.to_thread(storage.save_message, conversation, message.id, message.author.id, 'user', content)
            if not saved:
                return
            settings = await asyncio.to_thread(storage.preferences,message.author.id)
            status = await message.reply(progress_text('📚 One moment—checking the archives!',settings['personality']), mention_author=False)
            async def progress(text):
                try:
                    await status.edit(content=progress_text(text,settings['personality']))
                except discord.HTTPException:
                    LOG.warning('Could not update progress message.')
            try:
                async with message.channel.typing():
                    async with asyncio.timeout(240):
                        answer = await self.assistant.answer(conversation, message.id, progress, settings=settings)
                if isinstance(answer, DatasheetDisplay):
                    for document in answer.documents:
                        view = DatasheetView(document, message.author.id)
                        sent = await message.reply(embed=view.embed(),view=view,mention_author=False)
                        view.message = sent
                        record = ('Displayed datasheet card: Main (overview, abilities, weapons); Details (options, leaders, shared rules).\n'
                            f"{document['name']} [datasheet:{document['entity_id']}] {document.get('source_url') or ''}")
                        await asyncio.to_thread(storage.save_message,conversation,sent.id,self.user.id,'assistant',record)
                else:
                    sources=getattr(answer,'page_sources',[])[:5]
                    for number,chunk in enumerate(split_message(answer)):
                        source_view=None
                        if number==0 and sources:
                            source_view=discord.ui.View(timeout=None)
                            for source in sources:
                                source_view.add_item(discord.ui.Button(
                                    label=f"{source['title'][:45]} | PDF p. {source['page_start']}"[:80],url=source['viewer_url']))
                        sent = await message.reply(chunk, mention_author=False, suppress_embeds=True,view=source_view)
                        await asyncio.to_thread(storage.save_message, conversation, sent.id, self.user.id, 'assistant', chunk)
                await progress('✅ Archive check complete.')
            except Exception as exc:
                LOG.error('Question failed: %s', type(exc).__name__)
                await progress('⚠️ I couldn’t finish that lookup. Please try again. If it keeps happening, check the bot logs and API configuration.')

    async def on_raw_message_delete(self, payload):
        await asyncio.to_thread(storage.delete_message, payload.message_id)

    async def on_raw_bulk_message_delete(self, payload):
        for message_id in payload.message_ids:
            await asyncio.to_thread(storage.delete_message, message_id)

    async def on_raw_message_edit(self, payload):
        # Remove stale copies rather than retain outdated text or tool evidence.
        if str(payload.data.get('author',{}).get('id')) == str(self.user.id):
            return  # Card navigation edits must not erase saved conversation context.
        if 'content' in payload.data:
            await asyncio.to_thread(storage.delete_message, payload.message_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    load_env(ROOT/'.env')
    required = ['DISCORD_BOT_TOKEN', 'DISCORD_GUILD_ID', 'DISCORD_CHANNEL_ID', 'OPENAI_API_KEY']
    missing = [key for key in required if not os.getenv(key, '').strip()]
    if missing:
        raise ValueError('Missing configuration: ' + ', '.join(missing))
    for key in ('DISCORD_GUILD_ID', 'DISCORD_CHANNEL_ID'):
        if not os.environ[key].isdigit():
            raise ValueError(f'{key} must be a numeric Discord ID')
    storage.initialize()
    documents.initialize()
    storage.prune()
    if args.check:
        with storage.connect() as conn:
            snapshot = conn.execute('SELECT snapshot_id FROM snapshots WHERE is_active').fetchone()
        if not snapshot:
            raise ValueError('Import the rules before starting Lexi')
        print('Configuration present; rules database reachable; separate chat database ready. No Discord or model API calls made.')
        return
    Lexi().run(os.environ['DISCORD_BOT_TOKEN'], log_level=logging.WARNING)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    try:
        main()
    except Exception as exc:
        # Do not print driver/HTTP exception bodies that may contain credentials.
        if isinstance(exc, ValueError):
            LOG.error('%s', exc)
        else:
            LOG.error('Startup failed (%s). Check configuration, Message Content Intent, and service availability.', type(exc).__name__)
        raise SystemExit(1)
