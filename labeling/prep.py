# Подготовка данных для разметки.
#   python prep.py photos        # ../dataset/metadata.csv (датасет команды, 400 фото) → work/photos.json + пилот
#   python prep.py candidates    # ~90 доп. CC0-фото с iNaturalist НЕ из датасета → work/candidates.json для эталона
#
# Эталон (экзамены, обучение, контрольные ревью, примеры в инструкции) строим на отдельных фото:
# все 400 фото датасета должны честно пройти bbox и review, своя разметка их не заменяет.
import csv, json, os, random, collections, sys, time, urllib.parse, urllib.request

SEED = 2026
PILOT = 30            # фото в пилот: проверяем инструкцию и цепочку bbox → review до основного объёма
API = 'https://api.inaturalist.org/v1/observations'
UA = 'YSDA-butterfly-bbox/1.0 (course project)'
os.makedirs('work', exist_ok=True)
# в репозитории команды таблица лежит в ../dataset/, локальная копия — запасной вариант
META = next(p for p in ['../dataset/metadata.csv', 'data/metadata.csv'] if os.path.exists(p))


def cmd_photos():
    rows = list(csv.DictReader(open(META, encoding='utf-8')))
    assert len(rows) == 400 and len({r['photo_id'] for r in rows}) == 400
    assert all(r['license_code'] == 'cc0' for r in rows)
    # исполнителю отдаём large (≤1024 px): грузится быстро, нормированные координаты от размера не зависят
    photos = [{'photo_id': r['photo_id'], 'image': r['large_url'], 'group': r['group'], 'role': r['role']} for r in rows]
    rnd = random.Random(SEED)
    by = collections.defaultdict(list)
    for p in photos:
        by[p['group']].append(p)
    pilot = []
    for g, ps in sorted(by.items()):             # пропорционально группам, но хотя бы по одному из каждой
        rnd.shuffle(ps)
        pilot += ps[:max(1, round(PILOT * len(ps) / 400))]
    pilot_ids = {p['photo_id'] for p in pilot}
    for p in photos:
        p['batch'] = 'pilot' if p['photo_id'] in pilot_ids else 'main'
    json.dump(photos, open('work/photos.json', 'w'), ensure_ascii=False, indent=1)
    print('photos', len(photos), '| pilot', len(pilot), dict(collections.Counter(p['role'] for p in pilot)))


# сколько кандидатов какого вида тянуть. term 1 = Life Stage: 2 adult, 6 larva
CANDS = [
    ('butterfly', 47224, {'term_id': 1, 'term_value_id': 2}, 50),   # Papilionoidea, взрослые
    ('moth', 47157, {'term_id': 1, 'term_value_id': 2, 'without_taxon_id': 47224}, 15),  # Lepidoptera без бабочек
    ('larva', 47157, {'term_id': 1, 'term_value_id': 6}, 6),
    ('other_insect', 47158, {'without_taxon_id': 47157}, 10),         # Insecta без чешуекрылых
    ('flower', 47125, {}, 6),                                         # цветковые: кадр без насекомого
]


def get(params):
    url = API + '?' + urllib.parse.urlencode(params)
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            return json.load(urllib.request.urlopen(req, timeout=60))
        except Exception as e:
            print('retry', e); time.sleep(2 * (i + 1))
    sys.exit('iNaturalist недоступен')


def cmd_candidates():
    meta = list(csv.DictReader(open(META, encoding='utf-8')))
    used_ph, used_obs = {r['photo_id'] for r in meta}, {r['observation_id'] for r in meta}
    # датасет собран из самых свежих наблюдений; берём заметно более старые, чтобы не пересечься
    id_below = min(int(o) for o in used_obs) - 1_000_000
    rnd, out = random.Random(SEED), []
    for kind, taxon, extra, n in CANDS:
        got, page = [], 1
        while len(got) < n and page <= 5:
            res = get({'taxon_id': taxon, 'photos': 'true', 'photo_license': 'cc0', 'quality_grade': 'research',
                       'per_page': 100, 'page': page, 'order_by': 'id', 'order': 'desc', 'id_below': id_below, **extra})['results']
            page += 1
            rnd.shuffle(res)
            for o in res:
                ph = next((p for p in o.get('photos') or [] if (p.get('license_code') or '').lower() == 'cc0'), None)
                if not ph or str(ph['id']) in used_ph or str(o['id']) in used_obs or len(got) >= n:
                    continue
                used_ph.add(str(ph['id'])); used_obs.add(str(o['id']))
                got.append({'photo_id': 'x' + str(ph['id']), 'image': ph['url'].replace('/square.', '/large.'),
                            'kind': kind, 'taxon': (o.get('taxon') or {}).get('name'),
                            'observation_url': f"https://www.inaturalist.org/observations/{o['id']}",
                            'attribution': ph.get('attribution'), 'license': 'cc0'})
            time.sleep(1)
        print(kind, len(got))
        out += got
    rnd.shuffle(out)
    json.dump(out, open('work/candidates.json', 'w'), ensure_ascii=False, indent=1)
    print('candidates', len(out), '→ work/candidates.json (id с префиксом x, чтобы не путать с датасетом)')


def make_tool(data, out, title, key, prefix):
    # локальный инструмент рамок: фото вшиты в html, открывается двойным кликом, сервер не нужен.
    # prefix — начало имени экспортируемого файла: gold_<имя>.json, fix_<имя>.json
    html = (open('gold_tool.template.html', encoding='utf-8').read()
            .replace('/*DATA*/[]', json.dumps(data, ensure_ascii=False))
            .replace('/*TITLE*/Эталон рамок', title)
            .replace("'/*KEY*/gold_tool_v1'", repr(key))
            .replace('/*PREFIX*/gold_', prefix))
    open(out, 'w', encoding='utf-8').write(html)
    print(f'{out}: {len(data)} фото')


def cmd_tool():
    make_tool(json.load(open('work/candidates.json')), 'gold_tool.html', 'Эталон рамок', 'gold_tool_v1', 'gold_')


if __name__ == '__main__':
    {'photos': cmd_photos, 'candidates': cmd_candidates, 'tool': cmd_tool}[sys.argv[1]]()
