from pathlib import Path
import unittest

import pymupdf

from scripts.pdf_rules import dice_faces,native_page,numbered_sections


class VectorDiceTests(unittest.TestCase):
    def test_recovers_pips_from_graphics_without_hardcoded_rule_values(self):
        patterns=[[(1,1)],[(0,0),(2,2)],[(0,0),(1,1),(2,2)],
            [(0,0),(2,0),(0,2),(2,2)],[(0,0),(2,0),(1,1),(0,2),(2,2)],
            [(0,0),(0,1),(0,2),(2,0),(2,1),(2,2)]]
        with pymupdf.open() as pdf:
            page=pdf.new_page()
            for i,pattern in enumerate(patterns):
                x=30+i*30;y=30;size=12
                page.draw_rect(pymupdf.Rect(x,y,x+size,y+size),radius=.1,color=None,fill=(.1,.1,.1))
                for gx,gy in pattern:
                    page.draw_circle(pymupdf.Point(x+(.21,.5,.79)[gx]*size,y+(.21,.5,.79)[gy]*size),
                        radius=size*.097,color=None,fill=(.8,.8,.8))
            self.assertEqual([f['value'] for f in dice_faces(page)],[1,2,3,4,5,6])

    def test_numbered_rule_continuation_keeps_both_source_pages(self):
        pages=[{'page':1,'printed_label':'1','context':'','numbered_rules':[
            {'reference':'24.23','title':'[LETHAL HITS]','text':'Condition.','continuation':False}]},
            {'page':2,'printed_label':'2','context':'Sidebar.','numbered_rules':[
            {'reference':'24.23','title':'[LETHAL HITS]','text':'Exception.','continuation':True}]}]
        rules=numbered_sections(pages)
        section=next(s for s in rules if s['heading_path'].startswith('24.23'))
        self.assertEqual([s['page'] for s in section['spans']],[1,2])
        self.assertIn('Exception.',section['spans'][1]['text'])


CORE=Path(__file__).resolve().parents[1]/'Data/Documents/wh-40k-corerules.pdf'


@unittest.skipUnless(CORE.exists(),'Core Rules fixture not present')
class CoreRulesExtractionTests(unittest.TestCase):
    def test_attack_tables_have_recovered_numbers_and_no_replacement_characters(self):
        with pymupdf.open(CORE) as pdf:
            page,_=native_page(pdf[17],18)
        self.assertEqual(sorted(face['value'] for face in page['extraction_metadata']['dice_faces']),[1,1,2,3,4,5,6,6,6])
        self.assertEqual(page['extraction_metadata']['unplaced_dice'],[])
        self.assertNotIn('\ufffd',page['text'])
        self.assertIn('2+',page['text'])
        self.assertIn('6+',page['text'])

    def test_lethal_hits_is_a_complete_numbered_rule_with_designers_note(self):
        with pymupdf.open(CORE) as pdf:
            page,_=native_page(pdf[81],82)
        rule=next(r for r in page['numbered_rules'] if r['reference']=='24.23')
        self.assertIn('automatically wound',rule['text'])
        self.assertIn('no wound roll',rule['text'])
        self.assertIn('DEVASTATING',rule['text'])
        self.assertNotIn('~~',rule['text'])


if __name__=='__main__':
    unittest.main()
