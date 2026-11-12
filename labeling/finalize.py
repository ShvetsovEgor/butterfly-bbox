# Golden set из результатов краудсорсинга + ручной разбор командой.
#   python finalize.py queue     # fix_tool.html: (а) фото на ручной разбор, (б) случайная выборка принятых для самопроверки
#   python finalize.py build     # work/fix_*.json + ledger → ../golden/: golden.jsonl, golden_meta.csv, corrections.tsv, сырые ответы
#
# Кто отвечает за финальную разметку: принятые ревью ответы исполнителей; фото, где краудсорсинг не справился
# (2 отклонения подряд, «не уверен», битая картинка), и выборку принятых проверяет команда — её решение финальное.
# Модели в исправлениях не используются (если будете — отмечайте в комментарии «model», это попадёт в source).
import glob, json, os, random, sys, collections, csv
from boxes import match, counts
from prep import make_tool

SPOT_CHECK = 40          # ≈10% принятых: самопроверка качества итоговой разметки
OUT = os.environ.get('GOLDEN_DIR', '../golden')


def ledger():
    return json.load(open('work/ledger.json'))


def last_answer(led, pid, decision_prefix=None):
    anns = [a for a in led['ann'].values() if a['photo_id'] == pid and (not decision_prefix or (a['decision'] or '').startswith(decision_prefix))]
    return max(anns, key=lambda a: a['round']) if anns else None


def cmd_queue():
    led, rnd = ledger(), random.Random(2026)
    photos = {p['photo_id']: p for p in json.load(open('work/photos.json'))}
    manual, accepted = [], []
    for pid, ph in led['photos'].items():
        if ph.get('final') == 'manual':
            a = last_answer(led, pid)
            why = [l[3] for l in led['log'] if l[1] == pid and l[2] == 'manual']
            manual.append({'photo_id': pid, 'image': ph['image'], 'group': photos[pid]['group'], 'pre': a['boxes'] if a else [],
                           'note': 'РАЗБОР: ' + '; '.join(why) + (f' | последний ответ: {a["status"]}' if a else ''), 'queue': 'manual'})
        elif ph.get('final'):
            a = led['ann'][ph['final']]
            accepted.append({'photo_id': pid, 'image': ph['image'], 'group': photos[pid]['group'], 'pre': a['boxes'],
                             'note': f'ВЫБОРОЧНАЯ ПРОВЕРКА: принято {a["decision"]}', 'queue': 'spot'})
    # в выборку — все принятые 2:1 (самые спорные) и случайные единогласные до SPOT_CHECK
    maj = [x for x in accepted if 'majority' in x['note']]
    uni = [x for x in accepted if 'majority' not in x['note']]
    rnd.shuffle(uni)
    spot = maj + uni[:max(0, SPOT_CHECK - len(maj))]
    data = manual + spot
    json.dump(data, open('work/fix_queue.json', 'w'), ensure_ascii=False, indent=1)
    make_tool(data, 'fix_tool.html', 'Разбор и проверка', 'fix_tool_v1', 'fix_')
    print(f'на разбор {len(manual)}, на выборочную проверку {len(spot)} (из них 2:1 — {len(maj)}). '
          'Откройте fix_tool.html: исправьте рамки, «Готово»/«Бабочек нет», в поле пояснения — что и почему. Экспорт → work/fix_<имя>.json')


def cmd_build():
    led = ledger()
    photos = json.load(open('work/photos.json'))
    queue = {q['photo_id']: q for q in json.load(open('work/fix_queue.json'))} if os.path.exists('work/fix_queue.json') else {}
    fixes = {}
    for f in sorted(glob.glob('work/fix_*.json')):
        if f.endswith('fix_queue.json'):
            continue
        for r in json.load(open(f)):
            if r.get('status') in ('ok', 'none'):
                fixes[r['photo_id']] = r
    os.makedirs(OUT, exist_ok=True)
    golden, meta, corr, missing = [], [], [], []
    spot_stats = collections.Counter()
    for p in photos:
        pid, ph = p['photo_id'], led['photos'].get(p['photo_id'], {})
        crowd = led['ann'][ph['final']] if ph.get('final') not in (None, 'manual') else (led['ann'].get(ph.get('accepted')) if ph.get('accepted') else None)
        before = crowd['boxes'] if crowd else (last_answer(led, pid) or {}).get('boxes', [])
        fx = fixes.get(pid)
        if fx:
            boxes = [{k: b[k] for k in ('left', 'top', 'width', 'height')} for b in fx['boxes'] if b['label'] == 'butterfly']
            tp, fp, fn = counts(before, boxes, 0.5)
            changed = fp or fn or any(x < 0.9 for *_, x in match(before, boxes, 0.5))
            q = queue.get(pid, {}).get('queue', 'manual')
            source = ('team_fix' if changed else 'team_confirmed') + ('_model' if 'model' in (fx.get('comment') or '').lower() else '')
            if q == 'spot':
                spot_stats['checked'] += 1; spot_stats['changed'] += bool(changed)
            if changed or q == 'manual':
                corr.append([pid, p['group'], q, len(before), len(boxes), fx.get('annotator', ''), fx.get('comment', '')])
        elif crowd and ph.get('final') != 'manual':
            boxes, source = crowd['boxes'], 'crowd_' + crowd['decision']
        else:
            missing.append(pid); continue
        golden.append({'photo_id': int(pid), 'boxes': boxes, 'error': None})
        anns = sorted((a for a in led['ann'].values() if a['photo_id'] == pid), key=lambda a: a['round'])
        votes = ' | '.join(''.join('+' if r['verdict'] == 'OK' else '-' for r in a['reviews'] if not r['self']) for a in anns)
        meta.append([pid, p['group'], p['role'], source, len(boxes), len(anns), votes])
    if missing:
        print(f'! нет итоговой разметки у {len(missing)} фото: {missing[:10]} — дождитесь cycle или разберите в fix_tool')
    with open(f'{OUT}/golden.jsonl', 'w') as f:
        f.writelines(json.dumps(g) + '\n' for g in golden)
    w = lambda name, head, rows: csv.writer(open(f'{OUT}/{name}', 'w', newline=''), delimiter='\t').writerows([head] + rows)
    w('golden_meta.tsv', ['photo_id', 'group', 'role', 'source', 'n_boxes', 'n_annotations', 'review_votes_by_round'], meta)
    w('corrections.tsv', ['photo_id', 'group', 'queue', 'boxes_before', 'boxes_after', 'who', 'comment'], corr)
    # сырьё для отчёта и репозитория: все ответы исполнителей и все вердикты ревьюеров
    with open(f'{OUT}/raw_bbox_answers.jsonl', 'w') as f:
        for aid, a in led['ann'].items():
            f.write(json.dumps({k: a[k] for k in ('photo_id', 'assignment_id', 'worker', 'round', 'status', 'boxes', 'doubt', 'decision')}, ensure_ascii=False) + '\n')
    with open(f'{OUT}/raw_reviews.jsonl', 'w') as f:
        for aid, a in led['ann'].items():
            for r in a['reviews']:
                f.write(json.dumps({'bbox_assignment_id': aid, 'photo_id': a['photo_id'], **r}, ensure_ascii=False) + '\n')
    json.dump(led['log'], open(f'{OUT}/pipeline_log.json', 'w'), ensure_ascii=False, indent=1)
    src = collections.Counter(m[3] for m in meta)
    print(f'golden: {len(golden)}/400 фото, рамок {sum(len(g["boxes"]) for g in golden)}, '
          f'пустых {sum(not g["boxes"] for g in golden)} → {OUT}/golden.jsonl')
    print('источник разметки:', dict(src))
    if spot_stats['checked']:
        print(f'выборочная проверка: {spot_stats["checked"]} фото, исправлено {spot_stats["changed"]} '
              f'({spot_stats["changed"] / spot_stats["checked"]:.0%}) — примеры в {OUT}/corrections.tsv')


if __name__ == '__main__':
    {'queue': cmd_queue, 'build': cmd_build}[sys.argv[1]]()
