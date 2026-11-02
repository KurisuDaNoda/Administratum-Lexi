import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from lexi.datasheets import sections, DatasheetView, feel_no_pain


def document():
    return {'name':'Test Unit','entity_id':'1','snapshot_id':'snapshot','source_url':'https://example.com/unit',
        'content': {'model_profiles':[{'name':'Model','M':'6"','T':'3','Sv':'3+','inv_sv':'4','W':'1','Ld':'7+','OC':'1'}],
        'weapon_profiles':[{'name':'Test weapon','range':'12','A':'D6','BS_WS':'3','S':'4','AP':'-1','D':'2','description':'Pistol, Lethal Hits'}],
        'points':[{'description':'FIRST UNIT','cost':''},{'description':'5 models','cost':'75'}],
        'abilities':[{'name':'Protection','type':'Datasheet','description':'Only while a character is leading this unit.'},
            {'type':'Faction','definition':{'name':'Army Rule','content':{'description':'Full shared rule.'}}}],
        'equipment_options':[{'description':'Replace weapon only on the leader.'}],
        'leader_footer':'Only one attached leader.', 'can_be_led_by':[{'name':'Leader'}]}}


class RenderingTests(unittest.TestCase):
    def test_fnp_is_optional_and_respects_conditions_models_and_external_grants(self):
        doc=document()
        model=doc['content']['model_profiles'][0]
        self.assertNotIn('FNP','\n'.join(sections(doc)['Overview']))
        doc['content']['abilities']=[{'type':'Core','parameter':'6+','model':'Model','definition':{'name':'Feel No Pain'}}]
        self.assertEqual(feel_no_pain(doc['content'],model),('6+',()))
        self.assertEqual(feel_no_pain(doc['content'],model | {'name':'Other'}),('',()))
        doc['content']['abilities']=[{'name':'Protection','type':'Datasheet','description':'Models in this unit have the Feel No Pain 4+ ability against Psychic Attacks and mortal wounds.'}]
        fnp,notes=feel_no_pain(doc['content'],model)
        self.assertEqual(fnp,'4+*')
        self.assertIn('against Psychic Attacks and mortal wounds',notes[0])
        self.assertIn('FNP','\n'.join(sections(doc)['Overview']))
        doc['content']['abilities'][0]['description']='While this model is leading a unit, models in that unit have the Feel No Pain 5+ ability.'
        self.assertEqual(feel_no_pain(doc['content'],model),('',()))

    def test_ranged_and_melee_are_separate_and_unknown_profiles_are_retained(self):
        doc=document()
        base=doc['content']['weapon_profiles'][0]
        doc['content']['weapon_profiles']=[base | {'name':'Sword','type':'Melee','range':'Melee'},
            base | {'name':'Rifle','type':'Ranged'}, base | {'name':'Unknown'}]
        rendered='\n'.join(sections(doc)['Weapons'])
        self.assertLess(rendered.index('Ranged weapons'), rendered.index('Rifle'))
        self.assertLess(rendered.index('Rifle'), rendered.index('Melee weapons'))
        self.assertLess(rendered.index('Melee weapons'), rendered.index('Sword'))
        self.assertIn('Other weapon profiles',rendered)
        self.assertIn('Unknown',rendered)
        self.assertIn('WS',rendered)
        self.assertIn('BS',rendered)

    def test_duplicate_costs_are_removed_only_within_their_price_band(self):
        doc=document()
        doc['content']['points']=[{'description':'FIRST TWO','cost':''},
            {'description':'5 models','cost':'90'},{'description':'5 models','cost':'90'},
            {'description':'THIRD ONWARD','cost':''},{'description':'5 models','cost':'100'},
            {'description':'FIRST TWO','cost':''},{'description':'5 models','cost':'90'}]
        costs='\n'.join(sections(doc)['Costs & bases'])
        self.assertEqual(costs.count('90 pts'),1)
        self.assertEqual(costs.count('100 pts'),1)
        self.assertEqual(costs.count('FIRST TWO'),1)

    def test_tables_have_all_stats_and_keep_scope_and_price_headers(self):
        pages=sections(document())
        overview='\n'.join(pages['Overview'])
        self.assertIn('```text',overview)
        costs='\n'.join(pages['Costs & bases'])
        self.assertIn('FIRST UNIT',costs)
        self.assertIn('75 pts',costs)
        self.assertNotIn('75 pts',overview)
        weapons='\n'.join(pages['Weapons'])
        for value in ('Range','BS/WS','D6','-1','Pistol, Lethal Hits'):
            self.assertIn(value,weapons)
        abilities='\n'.join(pages['Abilities'])
        self.assertIn('Only while a character',abilities)
        self.assertNotIn('Full shared rule.',abilities)
        self.assertIn('Full shared rule.','\n'.join(pages['Shared rules']))
        self.assertIn('Only one attached leader.','\n'.join(pages['Leaders']))


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_sections_and_source_without_unit_navigation(self):
        view=DatasheetView(document(),123)
        interaction=NS(user=NS(id=123),response=NS(edit_message=AsyncMock(),send_message=AsyncMock()))
        self.assertTrue(await view.interaction_check(interaction))
        self.assertIn('Test weapon',view.embed().description)
        self.assertIn('Only while a character',view.embed().description)
        self.assertNotIn('Snapshot',str(view.embed().to_dict()))
        self.assertNotIn('expire',str(view.embed().to_dict()))
        self.assertEqual([b.label for b in view.children],['Main','Costs & bases','Details','Source ↗'])
        await next(b for b in view.children if b.label=='Details').callback(interaction)
        self.assertEqual(view.section,'Details')
        self.assertIn('Replace weapon only',view.embed().description)
        self.assertIn('Full shared rule.',view.embed().description)
        self.assertEqual(next(b.url for b in view.children if b.url),'https://example.com/unit')
        self.assertLessEqual(len(view.embed()),6000)
        view.stop()

    async def test_other_users_cannot_change_card_and_timeout_disables_controls(self):
        view=DatasheetView(document(),123)
        interaction=NS(user=NS(id=999),response=NS(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        view.message=NS(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(b.disabled for b in view.children if not b.url))
        self.assertFalse(next(b.disabled for b in view.children if b.url))
        view.stop()
