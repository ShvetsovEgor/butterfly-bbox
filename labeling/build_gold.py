# Эталон команды (gold_tool.html → work/gold_<имя>.json) → экзамены, контрольные и примеры для инструкций.
#   python build_gold.py            # всё: work/gold.json, bbox_exam.json, rev_{train,exam,honey}.json, картинки, инструкции
#   python build_gold.py --exam-answers work/exam_answers.json   # + реальные ответы с экзамена bbox в контрольные ревью
#
# Эталон рисует команда на ОТДЕЛЬНЫХ фото (кандидаты prep.py, id с префиксом x), не на 400 фото датасета.
# Если фото разметили двое: совпало (тот же ответ, все рамки IoU ≥ 0.7) — берём среднее; не совпало — в gold_conflicts.tsv
# и в эталон не идёт, пока не разберёте (исправьте у одного из разметчиков и экспортируйте заново).
import glob, json, os, random, sys, collections, urllib.request
from boxes import match, same, iou

SEED = 2026
AGREE_IOU = 0.7
EXAMPLE_TAGS = ['single', 'multi', 'overlap', 'cropped', 'small', 'similar', 'none']   # по одному примеру в инструкцию
N_EXAM = 10                       # фото в экзамене bbox: одна страница, ~3 мин работы
EXAM_POS, EXAM_NEG = 6, 4         # с бабочками / без (из них похожие насекомые — в первую очередь)
REV_TRAIN_PH, REV_EXAM_PH = 8, 6  # фото на обучение и экзамен ревью; остальные — контрольные в основной пул ревью
# картинки для инструкций надо где-то хостить: кладём в репозиторий команды, ссылка на raw-файл
IMG_BASE = os.environ.get('IMG_BASE', 'https://raw.githubusercontent.com/ShvetsovEgor/butterfly-bbox/main/labeling/instruction_img')
HINT = {
    'ok': 'Разметка верна: все бабочки выделены отдельными плотными рамками, лишних рамок нет.',
    'ok_none': 'Верно: взрослых бабочек на фото нет, рамок быть не должно.',
    'missed': 'Ошибка: пропущена бабочка. Осмотрите весь кадр, включая края и фон.',
    'extra': 'Ошибка: лишняя рамка — на похожем насекомом, фоне или вторая рамка на ту же бабочку.',
    'loose': 'Ошибка: рамка неточная — обрезает видимую часть бабочки или сильно больше неё.',
}


def tb(bs):
    # рамки → формат field.image-annotation
    return [{'shape': 'rectangle', **{k: round(b[k], 5) for k in ('left', 'top', 'width', 'height')}} for b in bs]


def auto_tags(bf, ds):
    # теги примеров, которые однозначно следуют из рамок эталона (ручные теги разметчиков в приоритете)
    t = set()
    if len(bf) == 1:
        t.add('single')
    if len(bf) >= 2:
        t.add('multi')
    if any(iou(a, c) > 0 for i, a in enumerate(bf) for c in bf[i + 1:]):
        t.add('overlap')
    if any(b['left'] < 0.01 or b['top'] < 0.01 or b['left'] + b['width'] > 0.99 or b['top'] + b['height'] > 0.99 for b in bf):
        t.add('cropped')
    if any(b['width'] * b['height'] < 0.01 for b in bf):
        t.add('small')
    if not bf:
        t.add('similar' if ds else 'none')
    return sorted(t)


def load_gold():
    files = sorted(glob.glob('work/gold_*.json'))
    files = [f for f in files if not f.endswith(('gold_conflicts.json',))]
    if not files:
        sys.exit('нет work/gold_<имя>.json — экспортируйте из gold_tool.html')
    by = collections.defaultdict(list)
    for f in files:
        for r in json.load(open(f)):
            if r.get('status') in ('ok', 'none'):
                r['bf'] = [b for b in r['boxes'] if b['label'] == 'butterfly']
                r['ds'] = [b for b in r['boxes'] if b['label'] != 'butterfly']
                by[r['photo_id']].append(r)
    gold, conflicts, agree = [], [], []
    for pid, rs in by.items():
        base = rs[0]
        if len(rs) >= 2:
            a, b = rs[0], rs[1]
            m = match(a['bf'], b['bf'], 0.5)
            ok = a['status'] == b['status'] and len(m) == len(a['bf']) == len(b['bf']) and all(x >= AGREE_IOU for *_, x in m)
            agree.append((ok, [x for *_, x in m]))
            if not ok:
                conflicts.append((pid, a['annotator'], len(a['bf']), b['annotator'], len(b['bf']), base['image']))
                continue
            # среднее двух рамок — эталон точнее каждой по отдельности
            base = dict(a, bf=[{k: (a['bf'][i][k] + b['bf'][j][k]) / 2 for k in ('left', 'top', 'width', 'height')} for i, j, _ in m])
        gold.append({'photo_id': pid, 'image': base['image'], 'kind': base.get('kind'), 'tags': sorted(set(sum((r.get('tags') or [] for r in rs), []))),
                     'boxes': base['bf'], 'distractors': base['ds'], 'annotators': [r['annotator'] for r in rs],
                     'auto_tags': auto_tags(base['bf'], base['ds'])})
    if agree:
        ious = [x for _, xs in agree for x in xs]
        print(f'двойная разметка: {len(agree)} фото, совпало {sum(o for o, _ in agree)}, '
              f'средний IoU совпавших рамок {sum(ious) / max(1, len(ious)):.3f}')
    with open('work/gold_conflicts.tsv', 'w') as f:
        f.write('photo_id\tannotator_a\tboxes_a\tannotator_b\tboxes_b\timage\n')
        f.writelines('\t'.join(map(str, c)) + '\n' for c in conflicts)
    print(f'эталон: {len(gold)} фото (с бабочками {sum(bool(g["boxes"]) for g in gold)}), конфликтов {len(conflicts)} → work/gold_conflicts.tsv')
    return sorted(gold, key=lambda g: g['photo_id'])


def jitter(b, rnd, k=0.02):
    w, h = b['width'], b['height']
    l, t = b['left'] + rnd.uniform(-k, k) * w, b['top'] + rnd.uniform(-k, k) * h
    return clip({'left': l, 'top': t, 'width': w * (1 + rnd.uniform(-k, k)), 'height': h * (1 + rnd.uniform(-k, k))})


def clip(b):
    l, t = max(0.0, b['left']), max(0.0, b['top'])
    r, d = min(1.0, b['left'] + b['width']), min(1.0, b['top'] + b['height'])
    return {'left': l, 'top': t, 'width': r - l, 'height': d - t}


def grow(b, s):
    # s > 0 — раздуть каждую сторону на s·размер; s < 0 — сжать (обрезать крылья)
    return clip({'left': b['left'] - s * b['width'], 'top': b['top'] - s * b['height'],
                 'width': b['width'] * (1 + 2 * s), 'height': b['height'] * (1 + 2 * s)})


def variants(g, rnd):
    # контрольные задания ревью из одного эталонного фото; вердикт каждого проверяется boxes.same()
    gb, out = g['boxes'], []
    if gb:
        out.append(('ok', 'butterflies', [jitter(b, rnd) for b in gb]))
        i = rnd.randrange(len(gb))
        rest = gb[:i] + gb[i + 1:]
        out.append(('missed', 'butterflies' if rest else 'none', rest))
        out.append(('loose', 'butterflies', gb[:i] + [grow(gb[i], rnd.choice([0.35, -0.22]))] + gb[i + 1:]))
        if g['distractors']:
            out.append(('extra', 'butterflies', gb + [g['distractors'][0]]))
    else:
        out.append(('ok_none', 'none', []))
        if g['distractors']:
            out.append(('extra', 'butterflies', [rnd.choice(g['distractors'])]))
    items = []
    for kind, status, bs in out:
        want = kind.startswith('ok')
        if same(gb, bs) is not want:      # синтетика обязана быть однозначной (у края кадра «раздуть» может не выйти)
            continue
        items.append({'id': f"{g['photo_id']}_{kind}", 'photo_id': g['photo_id'], 'image': g['image'], 'boxes': tb(bs),
                      'status': status, 'verdict': 'OK' if want else 'BAD', 'kind': kind, 'hint': HINT[kind]})
    return items


def from_exam_answers(path, gold):
    # реальные ответы с экзамена bbox: вердикт считаем по эталону, пограничные (IoU 0.5–0.75) не берём
    gmap, items = {g['photo_id']: g for g in gold}, []
    for a in json.load(open(path)):
        g = gmap.get(a['photo_id'])
        if not g or a['status'] == 'load_error':
            continue
        v = same(g['boxes'], a['boxes'])
        if v is None:
            continue
        m = match(g['boxes'], a['boxes'], 0.5)
        kind = 'ok' if v else ('missed' if len(m) < len(g['boxes']) else 'extra' if len(m) < len(a['boxes']) else 'loose')
        if v and not g['boxes']:
            kind = 'ok_none'
        items.append({'id': f"{a['photo_id']}_ans{a['assignment_id'][-6:]}", 'photo_id': a['photo_id'], 'image': g['image'],
                      'boxes': tb(a['boxes']), 'status': a['status'], 'verdict': 'OK' if v else 'BAD', 'kind': kind,
                      'hint': HINT[kind], 'source': 'exam_answer'})
    return items


def render(img_url, boxes, red, out):
    from PIL import Image, ImageDraw
    os.makedirs('work/img', exist_ok=True)
    cache = 'work/img/' + img_url.split('/photos/')[1].replace('/', '_')
    if not os.path.exists(cache):
        urllib.request.urlretrieve(img_url, cache)
    im = Image.open(cache).convert('RGB')
    W, H = im.size
    d, lw = ImageDraw.Draw(im), max(3, W // 200)
    for bs, col, dash in ((boxes, (22, 220, 74), False), (red, (230, 30, 70), True)):
        for b in bs:
            x0, y0, x1, y1 = b['left'] * W, b['top'] * H, (b['left'] + b['width']) * W, (b['top'] + b['height']) * H
            if not dash:
                d.rectangle([x0, y0, x1, y1], outline=col, width=lw)
                continue
            step = lw * 4
            for x in range(int(x0), int(x1), step * 2):
                d.line([x, y0, min(x + step, x1), y0], fill=col, width=lw); d.line([x, y1, min(x + step, x1), y1], fill=col, width=lw)
            for y in range(int(y0), int(y1), step * 2):
                d.line([x0, y, x0, min(y + step, y1)], fill=col, width=lw); d.line([x1, y, x1, min(y + step, y1)], fill=col, width=lw)
    os.makedirs('instruction_img', exist_ok=True)
    im.save(f'instruction_img/{out}.jpg', quality=85)


def instruction(src, dst, have):
    html = open(src, encoding='utf-8').read()
    for tag in set(__import__('re').findall(r'\{\{EX:(\w+)\}\}', html)):
        rep = f'<p><img width="600" src="{IMG_BASE}/ex_{tag}.jpg"></p>' if tag in have else ''
        if tag not in have:
            print(f'  ! в {os.path.basename(dst)} нет примера «{tag}» — отметьте такое фото тегом в gold_tool')
        html = html.replace('{{EX:%s}}' % tag, rep)
    open(dst, 'w', encoding='utf-8').write(html)


def main():
    rnd = random.Random(SEED)
    gold = load_gold()
    json.dump(gold, open('work/gold.json', 'w'), ensure_ascii=False, indent=1)

    # 1. примеры для инструкции bbox: по одному фото на тег, дальше эти фото нигде не используются
    used, have = set(), set()
    for tag in EXAMPLE_TAGS:
        cands = [g for g in gold if tag in g['tags'] and g['photo_id'] not in used] or \
                [g for g in gold if tag in g['auto_tags'] and g['photo_id'] not in used]
        if tag == 'similar':        # пример «не выделяем» должен быть наглядным: крупный похожий объект, лучше моль
            cands.sort(key=lambda g: (g.get('kind') != 'moth', -max((d['width'] * d['height'] for d in g['distractors']), default=0)))
        g = cands[0] if cands else None
        if g:
            used.add(g['photo_id']); have.add(tag)
            render(g['image'], g['boxes'], g['distractors'] if tag == 'similar' else [], f'ex_{tag}')
    rest = [g for g in gold if g['photo_id'] not in used]
    rnd.shuffle(rest)

    # 2. экзамен bbox: 6 с бабочками (сложные — в приоритете), 4 без (похожие насекомые — в приоритете)
    hard = lambda g: -len(set(g['tags'] + g['auto_tags']) & {'multi', 'overlap', 'cropped', 'small'}) - (len(g['boxes']) > 1)
    pos = sorted([g for g in rest if g['boxes']], key=hard)
    neg = sorted([g for g in rest if not g['boxes']], key=lambda g: -bool(g['distractors']))
    exam = pos[:EXAM_POS] + neg[:EXAM_NEG]
    if len(exam) < N_EXAM:
        print(f'  ! на экзамен bbox только {len(exam)} фото из {N_EXAM}: разметьте ещё')
    json.dump([{'photo_id': g['photo_id'], 'image': g['image'], 'boxes': g['boxes']} for g in exam],
              open('work/bbox_exam.json', 'w'), ensure_ascii=False, indent=1)

    # 3. контрольные ревью: фото экзамена bbox тоже годятся — исполнители bbox и review не пересекаются
    photos = rest[:]
    rnd.shuffle(photos)
    split = {'rev_train': photos[:REV_TRAIN_PH], 'rev_exam': photos[REV_TRAIN_PH:REV_TRAIN_PH + REV_EXAM_PH],
             'rev_honey': photos[REV_TRAIN_PH + REV_EXAM_PH:]}
    out = {k: sum((variants(g, rnd) for g in v), []) for k, v in split.items()}
    if len(sys.argv) > 2 and sys.argv[1] == '--exam-answers':
        extra = from_exam_answers(sys.argv[2], gold)
        out['rev_honey'] += extra
        print(f'  + {len(extra)} реальных ответов с экзамена bbox в контрольные ревью')
    for k, items in out.items():
        rnd.shuffle(items)
        json.dump(items, open(f'work/{k}.json', 'w'), ensure_ascii=False, indent=1)
        c = collections.Counter(i['verdict'] for i in items)
        print(f'{k}: {len(items)} заданий, OK {c["OK"]} / BAD {c["BAD"]}', dict(collections.Counter(i['kind'] for i in items)))

    # 4. примеры для инструкции ревью — из обучения ревью (оно и так показывается исполнителям)
    shown = set()
    for kind in ['ok', 'missed', 'extra', 'loose']:
        pool = [i for i in out['rev_train'] + out['rev_honey'] if i['kind'] == kind]
        it = next((i for i in pool if i['photo_id'] not in shown), None) or (pool[0] if pool else None)   # разные кадры
        if it:
            shown.add(it['photo_id'])
            render(it['image'], it['boxes'], [], f'ex_review_{kind}'); have.add(f'review_{kind}')
    instruction('yt/instruction_bbox.html', 'yt/instruction_bbox_final.html', have)
    instruction('yt/instruction_review.html', 'yt/instruction_review_final.html', have)
    print(f'картинки: instruction_img/ ({len(have)}) — закоммитьте в репозиторий, ссылки вида {IMG_BASE}/ex_single.jpg')


if __name__ == '__main__':
    main()
