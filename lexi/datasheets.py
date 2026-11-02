"""Deterministic, paginated Discord datasheet cards."""
from dataclasses import dataclass
import re

import discord

from scripts.import_data import clean_html


@dataclass
class DatasheetDisplay:
    documents: list[dict]


def text(row, key):
    cleaned = clean_html(str(row.get(key + '_text') or row.get(key) or ''))[0].replace('```', "'''")
    return '\n'.join(line for line in cleaned.splitlines() if line.strip())


def table(headers, values):
    rows = [[str(v or '-') for v in row] for row in values]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    line = lambda row: ' '.join(v.ljust(w) for v, w in zip(row, widths)).rstrip()
    return '```text\n' + '\n'.join([line(headers), *map(line, rows)]) + '\n```'


def paginate(blocks, maximum=3600):
    """Keep tables/normal rule blocks together; split oversized prose without loss."""
    pages, current = [], ''
    for block in blocks:
        if not block:
            continue
        while len(block) > maximum:
            if current:
                pages.append(current)
                current = ''
            split = block.rfind('\n', 0, maximum)
            if split < maximum // 2:
                split = maximum
            pages.append(block[:split])
            block = block[split:]
        candidate = (current + '\n' + block) if current else block
        if len(candidate) > maximum:
            pages.append(current)
            current = block
        else:
            current = candidate
    if current:
        pages.append(current)
    return pages or ['No entries in this section.']


def feel_no_pain(body, model):
    """Extract only explicit self/unit grants, never a leader/aura grant to others."""
    entries = body.get('abilities', []) or [a for group in body.get('ability_groups',{}).values() for a in group]
    values, notes = [], []
    for ability in entries:
        scope = text(ability, 'model')
        if scope and scope.casefold() != text(model, 'name').casefold():
            continue
        definition = ability.get('definition') or {}
        name = ability.get('name') or definition.get('name') or definition.get('content',{}).get('name','')
        if re.fullmatch(r'feel no pain(?:\s+\d\+)?', name.strip(), re.I) and ability.get('type','').lower() == 'core':
            match = re.search(r'[2-6]\+', text(ability,'parameter') or name)
            if match:
                values.append(match.group())
            continue
        # Wargear/leader/selected-target benefits are kept in rules text, not
        # interpreted as innate characteristics. Unknown wording is not guessed.
        if ability.get('type','').lower() != 'datasheet':
            continue
        description = text(ability,'description')
        match = re.match(r'^(Models in this unit have|This unit has|This model has) (?:the )?Feel No Pain ([2-6]\+)(?: ability)?(?P<condition>[^.]*)\.?$',description,re.I)
        if not match:
            continue
        if match.group(1).lower() == 'this model has' and not scope and len(body.get('model_profiles',[])) != 1:
            continue
        condition = match.group('condition').strip()
        values.append(match.group(2) + ('*' if condition else ''))
        if condition:
            notes.append(f"* {name}: {description}")
    return '/'.join(dict.fromkeys(values)), tuple(dict.fromkeys(notes))


def sections(document):
    body = document['content']
    overview = []
    profiles = {}
    for model in body.get('model_profiles', []):
        fnp, notes = feel_no_pain(body, model)
        key = tuple(model.get(k, '') for k in ('M','T','Sv','inv_sv','W','Ld','OC','inv_sv_descr')) + (fnp, notes)
        profiles.setdefault(key, []).append(text(model,'name') or 'Model profile')
    for profile, names in profiles.items():
        headers, values = ['M','T','Sv','Inv','W','Ld','OC'], list(profile[:7])
        if profile[8]:
            headers.append('FNP')
            values.append(profile[8])
        block = '**' + ' / '.join(names) + '**\n' + table(headers,[values])
        if profile[7]:
            block += '\nInvulnerable save condition: ' + clean_html(profile[7])[0]
        if profile[9]:
            block += '\n' + '\n'.join(profile[9])
        overview.append(block)
    overview.append('**Composition**\n' + '\n'.join(text(row,'description') for row in body.get('unit_composition',[])))
    costs = []
    price_groups = {}
    current_group = 'Unit cost'
    # Source exports can repeat rows and whole bands. Deduplicate only within
    # the same heading; never merge prices from different conditional bands.
    for cost in body.get('points', []):
        if not cost.get('cost'):
            current_group = text(cost,'description') or 'Unit cost'
        else:
            values = price_groups.setdefault(current_group, [])
            value = (text(cost,'description'), cost['cost'])
            if value not in values:
                values.append(value)
    for heading, values in price_groups.items():
        costs.append('**' + heading + '**\n' + '\n'.join(f'{description}: {value} pts' for description,value in values))
    bases = {}
    for model in body.get('model_profiles',[]):
        base = (text(model,'base_size'),text(model,'base_size_descr'))
        bases.setdefault(base,[]).append(text(model,'name'))
    if bases:
        costs.append('**Bases**')
        for (size, notes), names in bases.items():
            label = 'All models' if len(bases)==1 else ' / '.join(names)
            costs.append(f'{label}: {size or "Not provided"}' + (f'\n{notes}' if notes else ''))
    overview.append('**Keywords**\n' + '; '.join(text(k,'keyword') + (f" ({text(k,'model')})" if k.get('model') else '') for k in body.get('keywords',[])))
    weapon_groups = {'ranged': [], 'melee': [], 'other': []}
    for weapon in body.get('weapon_profiles', []):
        kind = text(weapon,'type').strip().lower()
        group = kind if kind in {'ranged', 'melee'} else 'other'
        skill = {'ranged': 'BS', 'melee': 'WS'}.get(group, 'BS/WS')
        block = f"**{text(weapon,'name')}**\n" + table(
            ['Range','A',skill,'S','AP','D'], [[weapon.get(k,'') for k in ('range','A','BS_WS','S','AP','D')]])
        if text(weapon,'description'):
            block += '\n' + text(weapon,'description')
        if text(weapon,'dice'):
            block += '\nDice: ' + text(weapon,'dice')
        weapon_groups[group].append(block)
    weapons = []
    for key, title in [('ranged','Ranged weapons'),('melee','Melee weapons'),('other','Other weapon profiles')]:
        blocks = weapon_groups[key]
        if blocks:
            weapons.append(f'**{title}**\n' + blocks[0])
            weapons.extend(blocks[1:])
    abilities, shared = [], []
    entries = body.get('abilities', [])
    if not entries:
        entries = [a for group in body.get('ability_groups',{}).values() for a in group]
    for ability in entries:
        definition = ability.get('definition') or {}
        rule = definition.get('content', {})
        name = ability.get('name') or definition.get('name') or rule.get('name') or 'Unnamed ability'
        category = ability.get('type') or 'Other'
        scope = '; '.join(f'{label}: {text(ability,key)}' for key,label in [('model','Model'),('parameter','Parameter')] if text(ability,key))
        description = text(ability,'description') or text(rule,'description') or 'No definition provided in the export.'
        block = f'**{name}** | {category}' + (f'\n{scope}' if scope else '') + '\n' + description
        if category.lower() in {'core','faction'}:
            abilities.append(f'**{category}:** {name}' + (f' | {scope}' if scope else '') + ' (definition in Shared rules)')
            shared.append(block)
        else:
            abilities.append(block)
    if text(body,'damaged_description'):
        abilities.append(f"**Damaged: {text(body,'damaged_w')}**\n{text(body,'damaged_description')}")
    options = ['**Default loadout**\n' + text(body,'loadout'), '**Equipment options**']
    options.extend(text(row,'description') for row in body.get('equipment_options',[]))
    if text(body,'transport'):
        options.append('**Transport**\n' + text(body,'transport'))
    leaders = []
    for key,label in [('leader_head','Attachment restrictions'),('leader_footer','Additional attachment rules')]:
        if text(body,key):
            leaders.append(f'**{label}**\n{text(body,key)}')
    for key,label in [('can_lead','Can lead'),('can_be_led_by','Can be led by')]:
        if body.get(key):
            leaders.append('**'+label+'**\n' + '\n'.join('- '+text(row,'name') for row in body[key]))
    return {name: paginate(blocks) for name, blocks in [('Overview',overview),('Costs & bases',costs),('Weapons',weapons),
        ('Abilities',abilities),('Options',options),('Leaders',leaders),('Shared rules',shared)]}


def card_pages(document):
    parts = sections(document)
    def combine(names):
        blocks = []
        for name in names:
            blocks.append('**' + name + '**')
            blocks.extend(parts[name])
        return paginate(blocks, maximum=3900)
    return {'Main': combine(['Overview', 'Abilities', 'Weapons']),
            'Costs & bases': combine(['Costs & bases']),
            'Details': combine(['Options', 'Leaders', 'Shared rules'])}


class DatasheetView(discord.ui.View):
    def __init__(self, document, owner_id):
        super().__init__(timeout=900)
        self.document = document
        self.owner_id = owner_id
        self.section = 'Main'
        self.page = 0
        self.pages = card_pages(document)
        self.message = None
        self.refresh()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('These controls belong to the person who requested this datasheet. Ask Lexi for your own copy.', ephemeral=True)
            return False
        return True

    def embed(self):
        pages = self.pages[self.section]
        embed = discord.Embed(title=self.document['name'][:256], url=self.document.get('source_url'),
            description=pages[self.page], colour=0x9C6B99)
        if len(pages)>1:
            embed.set_footer(text=f'Part {self.page+1}/{len(pages)}')
        return embed

    def refresh(self):
        self.clear_items()
        for section in self.pages:
            button = discord.ui.Button(label=section, style=discord.ButtonStyle.primary if section==self.section else discord.ButtonStyle.secondary, row=0)
            async def select(interaction, section=section):
                self.section, self.page = section, 0
                await self.update(interaction)
            button.callback = select
            self.add_item(button)
        url = self.document.get('source_url')
        if url:
            self.add_item(discord.ui.Button(label='Source ↗',url=url,row=0))
        if len(self.pages[self.section])>1:
            for label, delta in [('Previous part',-1),('Next part',1)]:
                button = discord.ui.Button(label=label, row=1, disabled=not 0<=self.page+delta<len(self.pages[self.section]))
                async def page(interaction, delta=delta):
                    self.page += delta
                    await self.update(interaction)
                button.callback = page
                self.add_item(button)

    async def update(self, interaction):
        self.refresh()
        await interaction.response.edit_message(embed=self.embed(),view=self)

    async def on_timeout(self):
        for item in self.children:
            if not getattr(item,'url',None):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
