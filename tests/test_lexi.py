import asyncio
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from lexi.bot import should_respond, split_message
from lexi.assistant import Assistant, document_for_tool, fuse_results
from copy import deepcopy


class RoutingTests(unittest.TestCase):
    def message(self, guild=1, channel=2, bot=False, mentions=()):
        return NS(author=NS(bot=bot), content='Question', guild=NS(id=guild) if guild else None,
            channel=NS(id=channel, parent_id=None), mentions=[NS(id=i) for i in mentions], reference=None)

    def test_test_channel_mentions_dms_and_other_servers(self):
        self.assertTrue(should_respond(self.message(), 9, 1, 2))
        self.assertTrue(should_respond(self.message(guild=None), 9, 1, 2))
        self.assertTrue(should_respond(self.message(channel=3, mentions=[9]), 9, 1, 2))
        self.assertFalse(should_respond(self.message(channel=3), 9, 1, 2))
        self.assertFalse(should_respond(self.message(guild=5, mentions=[9]), 9, 1, 2))
        self.assertFalse(should_respond(self.message(bot=True), 9, 1, 2))

    def test_discord_message_limit_and_content(self):
        text = 'a'*2200 + '\n' + 'b'*2200
        chunks = split_message(text)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        self.assertEqual(''.join(chunks).replace('\n',''), text.replace('\n',''))


class AbilityGroupingTests(unittest.TestCase):
    def test_hybrid_ranking_rewards_both_methods_and_preserves_table_identity(self):
        a={'table':'datasheets','entity_id':'1','name':'A'}
        b={'table':'detachments','entity_id':'1','name':'B'}
        result=fuse_results([], [a,b], [b])
        self.assertEqual(result[0]['name'],'B')
        self.assertEqual(result[0]['match_methods'],['lexical','semantic'])
        self.assertEqual(len(result),2)
        self.assertEqual(fuse_results([a],[a,b],[b])[0]['name'],'A')

    def document(self):
        return {'entity_type': 'datasheet', 'name': 'Assault Intercessor Squad', 'source_url': 'https://example.com/unit',
            'content': {'weapon_profiles': [{'name': 'Chainsword'}], 'abilities': [
                {'type': 'Faction', 'name': '', 'parameter': '', 'definition': {
                    'name': 'Combat Doctrines', 'content': {'description': 'Complete faction rule'}}},
                {'type': 'Datasheet', 'name': 'Targeted Intercession', 'description': 'If this unit charged, add bonuses.'},
                {'type': 'Core', 'name': '', 'model': 'Sergeant', 'parameter': '6', 'definition': {
                    'content': {'name': 'Scouts', 'description': 'Complete core rule'}}},
                {'type': 'Wargear', 'name': 'Special equipment', 'description': 'Equipment restriction'}]}}

    def test_grouping_preserves_definitions_scopes_urls_and_original_document(self):
        original = self.document()
        before = deepcopy(original)
        result = document_for_tool(original)
        groups = result['content']['ability_groups']
        self.assertEqual([x['name'] for x in groups['datasheet']], ['Targeted Intercession'])
        self.assertEqual([x['name'] for x in groups['faction']], ['Combat Doctrines'])
        self.assertEqual(groups['faction'][0]['definition']['content']['description'], 'Complete faction rule')
        self.assertEqual(groups['core'][0]['name'], 'Scouts')
        self.assertEqual(groups['core'][0]['parameter'], '6')
        self.assertEqual(groups['core'][0]['model'], 'Sergeant')
        self.assertEqual(groups['other'][0]['type'], 'Wargear')
        self.assertEqual(result['source_url'], original['source_url'])
        self.assertEqual(result['content']['weapon_profiles'], original['content']['weapon_profiles'])
        self.assertNotIn('abilities', result['content'])
        self.assertEqual(original, before)

    def test_non_datasheets_and_complete_detachments_are_unchanged(self):
        doc = {'entity_type': 'detachment', 'content': {'rules': [1], 'enhancements': [2], 'stratagems': [3]}}
        self.assertEqual(document_for_tool(doc), doc)

    def test_search_preview_has_explicit_names_without_printing_full_definitions(self):
        row = {'name': 'Assault Intercessor Squad', 'document': self.document()}
        result = Assistant.previews([row], 'snapshot')['results'][0]
        self.assertEqual(result['ability_names_by_category']['faction'], ['Combat Doctrines'])
        self.assertFalse(result['preview_is_complete'])


class AssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_rules_corpus_reports_availability_not_a_missing_game_rule(self):
        assistant=Assistant.__new__(Assistant)
        assistant.model='test';assistant.max_rounds=2
        call=NS(type='function_call',name='search_rules',arguments='{"query":"lethal hits","edition":11}',call_id='rule-test')
        assistant.client=NS(responses=NS(create=AsyncMock(side_effect=[
            NS(output=[call],status='completed'),
            NS(output=[],output_text='That rule does not exist. Should I search the web?',status='completed')])))
        assistant.rules_tool=AsyncMock(return_value={'results':[],'coverage_status':'no_active_reviewed_rules'})
        conn=MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value=('snapshot',)
        with patch.dict(os.environ,{'LEXI_WEB_SEARCH_ENABLED':'false'}), \
             patch('lexi.assistant.storage.connect',return_value=conn), \
             patch('lexi.assistant.storage.history',return_value=[{'role':'user','content':'What are lethal hits?'}]), \
             patch('lexi.assistant.storage.log_tool'):
            answer=await assistant.answer('c','m',AsyncMock())
        self.assertIn('extraction review',answer)
        self.assertIn('Web search is disabled',answer)
        self.assertNotIn('does not exist',answer)
        self.assertNotIn('Should I search',answer)

    async def test_database_only_mode_hides_web_tool_and_blocks_forced_web_calls(self):
        assistant=Assistant.__new__(Assistant)
        assistant.model='test';assistant.max_rounds=2
        call=NS(type='function_call',name='search_web',arguments='{"query":"rules","edition":"11th"}',call_id='test-web')
        assistant.client=NS(responses=NS(create=AsyncMock(side_effect=[
            NS(output=[call],status='completed'),NS(output=[],output_text='No local evidence found.',status='completed')]),stream=MagicMock()))
        conn=MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value=('snapshot',)
        with patch.dict(os.environ,{'LEXI_WEB_SEARCH_ENABLED':'false'}), \
             patch('lexi.assistant.storage.connect',return_value=conn), \
             patch('lexi.assistant.storage.history',return_value=[{'role':'user','content':'Question'}]), \
             patch('lexi.assistant.storage.log_tool') as log:
            await assistant.answer('c','m',AsyncMock())
            direct=await assistant.web_tool('rules',AsyncMock())
        self.assertEqual(direct['error'],'web_search_disabled')
        self.assertEqual(log.call_args.args[-1]['error'],'web_search_disabled')
        assistant.client.responses.stream.assert_not_called()
        for call in assistant.client.responses.create.await_args_list:
            self.assertNotIn('search_web',[tool['name'] for tool in call.kwargs['tools']])
            self.assertIn('DATABASE-ONLY TEST MODE',call.kwargs['instructions'])

    async def test_all_scope_searches_all_tables_with_one_query_embedding(self):
        from scripts.import_data import TABLES
        assistant=Assistant.__new__(Assistant)
        assistant.client=NS(embeddings=NS(create=AsyncMock(return_value=NS(data=[NS(embedding=[1,0])]))))
        conn=MagicMock()
        conn.__enter__.return_value=conn
        conn.execute.return_value.fetchall.return_value=[]
        def lookup(connection,tables,snapshot,**kwargs):
            self.assertEqual(tables,list(TABLES))
            self.assertEqual(kwargs.get('faction'),'SM')
            if 'vector' not in kwargs:
                return []
            return [{'table':table,'entity_id':'same-id','name':table,'source_url':None,
                'document':{'entity_type':'detachment','name':table,'content':{}},'score':.8}
                for table in ['shared_rules','detachments']]
        with patch('lexi.assistant.storage.connect',return_value=conn), \
             patch('lexi.assistant.search',side_effect=lookup), \
             patch('lexi.assistant.embedding_configuration',return_value=('config','model',2)):
            result=await assistant.database_tool('search_database',{'query':'Deathblow','table':'all','faction_id':'SM'},'snapshot')
        self.assertEqual(result['searched_tables'],list(TABLES))
        self.assertEqual(len(result['results']),2)
        self.assertEqual(conn.execute.call_count,9)  # two read-only transactions + seven lexical tables
        assistant.client.embeddings.create.assert_awaited_once()

    @patch.dict(os.environ,{'LEXI_WEB_SEARCH_ENABLED':'true'})
    async def test_web_request_explicitly_scopes_default_and_requested_edition(self):
        class Stream:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
            async def get_final_response(self):
                return NS(output=[], output_text='No verified evidence.')
        assistant = Assistant.__new__(Assistant)
        assistant.model = 'test'
        stream = MagicMock(return_value=Stream())
        assistant.client = NS(responses=NS(stream=stream))
        for edition in ('11th', '10th'):
            await assistant.web_tool('How does this ability work?', AsyncMock(), edition)
            request = stream.call_args.kwargs
            self.assertIn(f'requested rules edition: {edition}', request['input'])
            self.assertIn('Discard incompatible editions', request['instructions'])
            self.assertIn('older-edition rules', request['input'])

    async def test_tool_retry_progress_and_final_answer(self):
        assistant = Assistant.__new__(Assistant)
        assistant.model = 'test'
        assistant.max_rounds = 3
        call = NS(type='function_call', name='search_database', arguments='{"query":"Warboss","table":"datasheets"}', call_id='1')
        response1 = NS(output=[call], output_text='', status='completed')
        response2 = NS(output=[], output_text='An answer [source](https://example.com).', status='completed')
        assistant.client = NS(responses=NS(create=AsyncMock(side_effect=[response1, response2])))
        assistant.database_tool = AsyncMock(return_value={'results': []})
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = ('snapshot',)
        progress = AsyncMock()
        with patch('lexi.assistant.storage.connect', return_value=conn), \
             patch('lexi.assistant.storage.history', return_value=[{'role':'user','content':'Question'}]), \
             patch('lexi.assistant.storage.log_tool') as log:
            answer = await assistant.answer('conversation', 'message', progress)
        self.assertIn('https://example.com', answer)
        progress.assert_awaited_once()
        assistant.database_tool.assert_awaited_once()
        log.assert_called_once()
        self.assertEqual(assistant.client.responses.create.await_count, 2)

    async def test_lookup_failure_is_returned_to_model(self):
        assistant = Assistant.__new__(Assistant)
        assistant.model = 'test'
        assistant.max_rounds = 1
        call = NS(type='function_call', name='get_document', arguments='{"entity_id":"1","table":"datasheets"}', call_id='1')
        assistant.client = NS(responses=NS(create=AsyncMock(side_effect=[
            NS(output=[call], status='completed'), NS(output=[], output_text='Evidence unavailable.', status='completed')])))
        assistant.database_tool = AsyncMock(side_effect=RuntimeError('private connection details'))
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = ('snapshot',)
        with patch('lexi.assistant.storage.connect', return_value=conn), \
             patch('lexi.assistant.storage.history', return_value=[]), patch('lexi.assistant.storage.log_tool') as log:
            await assistant.answer('c','m',AsyncMock())
        result = log.call_args.args[-1]
        self.assertEqual(result['error'], 'RuntimeError')
        self.assertNotIn('private', str(result))


if __name__ == '__main__':
    unittest.main()
