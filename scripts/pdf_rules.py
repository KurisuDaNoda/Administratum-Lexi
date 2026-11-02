"""Native, geometry-aware extraction for the numbered 11th-edition Core Rules.

No game rules are hardcoded. Dice values come from vector pip geometry; text
comes from the PDF text layer. The original PDF is never modified.
"""
import math
import re

import pymupdf

REFERENCE=re.compile(r'(.+?)\s+(\d{2}\.\d{2}(?:\.\d{2})?)\s*$')


def clean(value):
    value=value.replace('\x1e',' -> ').replace('\x16','.')
    return re.sub(r'[\x00-\x08\x0b-\x1f]','',value).replace('\u00ad','')


def dice_faces(page):
    drawings=page.get_drawings()
    faces=[]
    patterns={1:[{(1,1)}],2:[{(0,0),(2,2)},{(2,0),(0,2)}],
        3:[{(0,0),(1,1),(2,2)},{(2,0),(1,1),(0,2)}],
        4:[{(0,0),(2,0),(0,2),(2,2)}],5:[{(0,0),(2,0),(1,1),(0,2),(2,2)}],
        6:[{(0,0),(0,1),(0,2),(2,0),(2,1),(2,2)},
           {(0,0),(1,0),(2,0),(0,2),(1,2),(2,2)}]}
    for outer in drawings:
        r=outer['rect'];fill=outer.get('fill')
        if not (fill and max(fill)<.35 and 5<=r.width<=24 and abs(r.width-r.height)<.25 and len(outer['items'])==8):
            continue
        points=set();valid=True
        for inner in drawings:
            b=inner['rect'];colour=inner.get('fill')
            if not (colour and min(colour)>.55 and b in r and len(inner['items'])==4
                    and .1*r.width<b.width<.3*r.width and abs(b.width-b.height)<.15):
                continue
            x=((b.x0+b.x1)/2-r.x0)/r.width;y=((b.y0+b.y1)/2-r.y0)/r.height
            grid=lambda p:min(range(3),key=lambda i:abs(p-(.21,.5,.79)[i]))
            gx,gy=grid(x),grid(y)
            valid=valid and abs(x-(.21,.5,.79)[gx])<.1 and abs(y-(.21,.5,.79)[gy])<.1
            points.add((gx,gy))
        count=len(points)
        if valid and points in patterns.get(count,[]):
            faces.append({'bbox':list(r),'value':count})
    return faces


def native_page(page,number,previous=None):
    raw=page.get_text('dict')
    blocks=[];chapter=None
    for block_index,block in enumerate(raw['blocks']):
        if block.get('type')!=0:
            continue
        lines=[]
        for line_index,line in enumerate(block['lines']):
            if abs(line['dir'][1])>.02:  # Vertical chapter banners / rotated examples.
                continue
            spans=line['spans'];value=clean(''.join(s['text'] for s in spans)).strip()
            if re.fullmatch(r'\d{2}',value) and max(s['size'] for s in spans)>=25:
                chapter=value;continue
            if line['bbox'][1]>page.rect.height*.93 or not value:
                continue
            lines.append({'bbox':line['bbox'],'spans':list(spans),'text':value,
                          'order':(block['bbox'][1],block['bbox'][0],block_index,line_index),
                          'heading_font':any(('Akhand' in s['font'] or 'InputMono' in s['font']) and any(c.isalpha() for c in s['text']) for s in spans)})
        if lines:
            blocks.append({'bbox':block['bbox'],'lines':lines})
    faces=dice_faces(page);unplaced=[];diagram_dice=[]
    for face in faces:
        r=pymupdf.Rect(face['bbox']);cy=(r.y0+r.y1)/2
        choices=[]
        for block in blocks:
            for line in block['lines']:
                b=pymupdf.Rect(line['bbox']);dy=abs((b.y0+b.y1)/2-cy)
                dx=max(b.x0-r.x1,r.x0-b.x1,0)
                if dy<6 and dx<45:
                    choices.append((dy+dx*.12,line))
        if not choices:
            if r.width>16:
                diagram_dice.append(face)
            else:
                unplaced.append(face)
            continue
        line=min(choices,key=lambda item:item[0])[1]
        line['spans'].append({'bbox':face['bbox'],'text':' '+str(face['value'])+' '})
    for block in blocks:
        for line in block['lines']:
            value=clean(''.join(s['text'] for s in sorted(line['spans'],key=lambda s:s['bbox'][0])))
            line['text']=re.sub(r'\b(\d)\s+\+',r'\1+',re.sub(r'[ \t]+',' ',value)).strip()
    anchors=[]
    for block in blocks:
        for index,line in enumerate(block['lines']):
            match=REFERENCE.fullmatch(line['text'])
            if not match or not line['heading_font']:
                continue
            title,reference=match.groups()
            if not title.isupper() or (chapter and not reference.startswith(chapter+'.')):
                continue
            # Join a wrapped title such as "MONSTERS / AND VEHICLES".
            start=index
            while start>0:
                before=block['lines'][start-1]
                if not before['heading_font'] or not before['text'].isupper() or REFERENCE.fullmatch(before['text']):
                    break
                title=before['text']+' '+title;start-=1
            anchors.append({'reference':reference,'title':title,'x':block['lines'][start]['bbox'][0],
                'y':block['lines'][start]['bbox'][1], 'heading_lines':{id(l) for l in block['lines'][start:index+1]},'parts':[]})
    anchors.sort(key=lambda a:(a['x'],a['y']))
    columns=[]
    for anchor in anchors:
        if not any(abs(anchor['x']-x)<40 for x in columns):
            columns.append(anchor['x'])
    columns.sort()
    def column(anchor):
        return min(range(len(columns)),key=lambda i:abs(anchor['x']-columns[i]))
    anchors.sort(key=lambda a:(column(a),a['y']))
    context=[];continuation=[]
    for block in blocks:
        for line in block['lines']:
            owner=next((a for a in anchors if id(line) in a['heading_lines']),None)
            if owner:
                # Header emitted once below, not again as body text.
                continue
            b=pymupdf.Rect(line['bbox'])
            candidates=[]
            for a in anchors:
                col=column(a);right=columns[col+1]-4 if col+1<len(columns) else page.rect.width
                if (block['bbox'][0]>=columns[col]-12 and block['bbox'][0]<=columns[col]+50
                        and (b.x0+b.x1)/2<right and b.y0>=a['y']-1):
                    candidates.append(a)
            if candidates:
                selected=max(candidates,key=lambda a:(column(a),a['y']))
                selected['parts'].append((*line['order'],line['text']))
            elif previous and chapter and previous['reference'].startswith(chapter+'.') and (
                    not anchors or (b.x0>=columns[0]-12 and b.y0<anchors[0]['y'])):
                continuation.append((*line['order'],line['text']))
            else:
                context.append((*line['order'],line['text']))
    rules=[]
    if continuation:
        rules.append({'reference':previous['reference'],'title':previous['title'],
            'text':'\n'.join(v[4] for v in sorted(continuation)),'continuation':True})
    for a in anchors:
        rules.append({'reference':a['reference'],'title':a['title'],
            'text':'\n'.join(v[4] for v in sorted(a['parts'])),'continuation':False})
    full='\n\n'.join('\n'.join(line['text'] for line in block['lines']) for block in blocks)
    illustration=''
    if diagram_dice:
        illustration='Illustration dice faces (top to bottom, not a rule threshold): '+', '.join(
            str(d['value']) for d in sorted(diagram_dice,key=lambda d:(d['bbox'][1],d['bbox'][0])))
        full+='\n\n'+illustration
    result={'page':number,'printed_label':page.get_label() or None,'text':full,
        'numbered_rules':rules,'context':'\n'.join(v[4] for v in sorted(context))+'\n'+illustration,
        'extraction_metadata':{'method':'native-text-and-vector-pips','dice_faces':faces,
            'diagram_dice':diagram_dice,'unplaced_dice':unplaced,'replacement_characters':full.count('\ufffd')}}
    return result,anchors[-1] if anchors else previous


def extract_numbered(data):
    pages=[];previous=None
    with pymupdf.open(stream=data,filetype='pdf') as pdf:
        if pdf.needs_pass:
            raise ValueError('Encrypted PDF requires a decrypted copy')
        for index,page in enumerate(pdf):
            result,previous=native_page(page,index+1,previous)
            pages.append(result)
            print(f'Extracted page {index+1}/{len(pdf)}; recovered {len(result["extraction_metadata"]["dice_faces"])} dice faces',flush=True)
    return pages


def numbered_sections(pages):
    sections=[];by_reference={}
    for page in pages:
        span=lambda body:{'page':page['page'],'printed_label':page['printed_label'],'text':body}
        if page.get('context','').strip():
            sections.append({'heading_path':f'PDF page {page["page"]} context','spans':[span(page['context'])]})
        for rule in page['numbered_rules']:
            reference=rule['reference']
            if reference not in by_reference:
                section={'heading_path':reference+' - '+rule['title'],'spans':[]}
                by_reference[reference]=section;sections.append(section)
            heading='' if rule['continuation'] else '## '+reference+' '+rule['title']+'\n\n'
            by_reference[reference]['spans'].append(span(heading+rule['text']))
    return sections
