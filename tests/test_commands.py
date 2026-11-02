import asyncio
from collections import defaultdict
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import discord

from lexi.commands import install_commands, conversation_id, preference_instructions, preference_message, progress_text, evidence_urls


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=discord.Client(intents=discord.Intents.none())
        self.client.guild_id=123
        self.client.locks=defaultdict(asyncio.Lock)
        self.tree=install_commands(self.client,owner_id=999)
        self.commands={c.name:c for c in self.tree.get_commands(guild=discord.Object(id=123))}
        self.interaction=NS(user=NS(id=1),guild_id=123,channel_id=456,
            response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))

    async def asyncTearDown(self):
        await self.client.close()

    async def test_only_requested_commands_registered(self):
        self.assertEqual(set(self.commands),{'help','personality','prompt','forget','settings','sources','context','debug'})
        self.assertTrue(all(c.to_dict(self.tree)['name']==name for name,c in self.commands.items()))

    async def test_debug_denied_before_reading_history(self):
        with patch('lexi.commands.storage.latest_trace') as read:
            await self.commands['debug'].callback(self.interaction,'someone-elses-question')
        read.assert_not_called()
        self.assertTrue(self.interaction.followup.send.call_args.kwargs['ephemeral'])
        self.assertIn('restricted',self.interaction.followup.send.call_args.args[0])

    async def test_prompt_uses_calling_user_and_private_response(self):
        settings={'custom_prompt':'Use short answers.'}
        with patch('lexi.commands.storage.set_preferences',return_value=settings) as save:
            await self.commands['prompt'].callback(self.interaction,'set','Use short answers.')
        save.assert_called_once_with(1,custom_prompt='Use short answers.')
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
        self.assertTrue(self.interaction.followup.send.call_args.kwargs['ephemeral'])

    async def test_changing_faction_clears_old_detachment(self):
        with patch('lexi.commands.storage.set_preferences',return_value={'faction':'Orks','detachment':''}) as save:
            await self.commands['context'].callback(self.interaction,'set','Orks')
        save.assert_called_once_with(1,faction='Orks',detachment='')

    async def test_forget_does_not_delete_preferences_and_respects_active_answer(self):
        key=conversation_id(123,456,1)
        await self.client.locks[key].acquire()
        try:
            with patch('lexi.commands.storage.clear') as clear:
                await self.commands['forget'].callback(self.interaction)
                clear.assert_not_called()
        finally:
            self.client.locks[key].release()
        with patch('lexi.commands.storage.clear') as clear:
            await self.commands['forget'].callback(self.interaction)
        clear.assert_called_once_with(key)

    async def test_prompt_length_is_validated(self):
        with patch('lexi.commands.storage.set_preferences') as save:
            await self.commands['prompt'].callback(self.interaction,'set','x'*1501)
        save.assert_not_called()


class PreferencesTests(unittest.TestCase):
    def test_preference_data_is_user_role_and_neutral_has_no_roleplay(self):
        settings={'personality':'neutral','custom_prompt':'Keep it short','faction':'Orks','detachment':'War Horde'}
        self.assertIn('No roleplay',preference_instructions(settings))
        self.assertEqual(preference_message(settings)['role'],'user')
        self.assertIn('War Horde',preference_message(settings)['content'])
        self.assertEqual(progress_text('📚 One moment—checking the archives!','neutral'),'Checking the relevant rules…')

    def test_sources_are_deduplicated_and_do_not_include_arbitrary_text(self):
        self.assertEqual(evidence_urls({'source_url':'https://example.com','other':{'url':'https://example.com'},'content':'untrusted link'}),['https://example.com'])
