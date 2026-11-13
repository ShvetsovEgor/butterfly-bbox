# Сквозная проверка без платформы: фейковый API Яндекс Заданий + симулированные исполнители.
#   python tests/test_pipeline.py
# Все рамки здесь СЛУЧАЙНЫЕ (синтетическая «истина» для теста), это не разметка датасета.
import json, os, random, shutil, sys, tempfile, collections, itertools

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
rnd = random.Random(7)


# ---------------- фейковый бэкенд ----------------
class Fake:
    def __init__(self):
        self.ids = itertools.count(1000)
        self.pools, self.tasks, self.asg, self.skills, self.user_skills = {}, {}, {}, {}, {}
        self.calls = collections.Counter()

    def nid(self):
        return str(next(self.ids))

    def page(self, items, params):
        items = sorted(items, key=lambda x: int(x['id']))
        if params.get('id_gt'):
            items = [x for x in items if int(x['id']) > int(params['id_gt'])]
        lim = params.get('limit', 1000)
        return {'items': items[:lim], 'has_more': len(items) > lim}

    def __call__(self, method, path, body=None, params=None):
        params = params or {}
        self.calls[(method, path.split('/')[1])] += 1
        parts = path.strip('/').split('/')
        if method == 'GET' and path == '/requester':
            return {'balance': 850}
        if method == 'POST' and path == '/skills':
            i = self.nid(); self.skills[i] = body; return {'id': i}
        if method == 'POST' and path == '/projects':
            for f in ('public_instructions', 'task_spec'):
                assert body.get(f), f
            json.loads(body['task_spec']['view_spec']['config'])
            return {'id': self.nid()}
        if method == 'POST' and path in ('/pools', '/trainings'):
            i = self.nid(); self.pools[i] = dict(body, id=i, status='CLOSED', kind=path[1:]); return {'id': i}
        if method == 'POST' and parts[0] in ('pools', 'trainings') and parts[-1] in ('open', 'close'):
            self.pools[parts[1]]['status'] = 'OPEN' if parts[-1] == 'open' else 'CLOSED'; return {}
        if method == 'GET' and parts[0] == 'pools':
            return self.pools[parts[1]]
        if method == 'POST' and path == '/tasks':
            assert len(body) <= 100, 'пачки по 100'
            for t in body:
                assert t['pool_id'] in self.pools, t['pool_id']
                assert ('overlap' in t) != ('infinite_overlap' in t)
                i = self.nid(); self.tasks[i] = dict(t, id=i)
            return {}
        if method == 'GET' and path == '/tasks':
            return self.page([t for t in self.tasks.values() if t['pool_id'] == params['pool_id']], params)
        if method == 'GET' and path == '/assignments':
            return self.page([a for a in self.asg.values() if a['pool_id'] == params['pool_id']], params)
        if method == 'GET' and parts[0] == 'assignments' and len(parts) == 2:
            return self.asg[parts[1]]
        if method == 'PATCH' and parts[0] == 'assignments':
            a = self.asg[parts[1]]
            if a['status'] != 'SUBMITTED':          # как платформа: 409 → api.py делает sys.exit
                raise SystemExit(f'409 {a["status"]}')
            a['status'] = body['status']; return a
        if method == 'PUT' and path == '/user-skills':
            self.user_skills[(body['user_id'], body['skill_id'])] = body['value']; return {}
        raise AssertionError(f'неизвестный вызов {method} {path}')

    # --- исполнители ---
    def allowed(self, pool, w):
        for f in pool['filter']['and']:
            if f['category'] != 'skill':
                continue
            v = self.user_skills.get((w, f['key']))
            if f['operator'] == 'EQ' and 'value' not in f and v is not None:
                return False
            if f['operator'] == 'GTE' and (v is None or v < f['value']):
                return False
        return True

    def take(self, pool_id, w, solve):
        # одна страница: реальные задания с недобранным перекрытием, которые w ещё не делал, + контрольные
        pool = self.pools[pool_id]
        if pool['status'] != 'OPEN' or not self.allowed(pool, w):
            return False
        done_by = collections.defaultdict(set); used = collections.Counter()
        for a in self.asg.values():
            if a['pool_id'] == pool_id:
                for t in a['tasks']:
                    done_by[t['id']].add(a['user_id'])
                    if a['status'] != 'EXPIRED':
                        used[t['id']] += 1
        mix = pool['mixer_config']
        real = [t for t in self.tasks.values() if t['pool_id'] == pool_id and not t.get('known_solutions')
                and used[t['id']] < t['overlap'] and w not in done_by[t['id']]]
        gold = [t for t in self.tasks.values() if t['pool_id'] == pool_id and t.get('known_solutions') and w not in done_by[t['id']]]
        real = sorted(real, key=lambda t: int(t['id']))[:mix['real_tasks_count']]
        gold = gold[:mix['golden_tasks_count']]
        if not real and (mix['real_tasks_count'] or not gold):
            return False
        tasks = real + gold
        i = self.nid()
        self.asg[i] = {'id': i, 'pool_id': pool_id, 'user_id': w, 'reward': pool['reward_per_assignment'],
                       'status': 'ACCEPTED' if pool['auto_accept_solutions'] else 'SUBMITTED',
                       'tasks': [{'id': t['id'], 'input_values': t['input_values']} for t in tasks],
                       'solutions': [{'output_values': solve(t)} for t in tasks]}
        return True


# ---------------- синтетическая «истина» и исполнители ----------------
def rbox():
    w, h = rnd.uniform(0.1, 0.4), rnd.uniform(0.1, 0.4)
    return {'left': rnd.uniform(0, 1 - w), 'top': rnd.uniform(0, 1 - h), 'width': w, 'height': h}


def noisy(truth, q):
    # исполнитель качества q: иногда пропускает, добавляет лишнюю, рисует неточно
    out = [dict(b) for b in truth if rnd.random() > (1 - q) * 0.5]
    out = [{'left': b['left'] + rnd.gauss(0, 0.01 + (1 - q) * 0.05) * b['width'], 'top': b['top'], 'width': b['width'], 'height': b['height']} for b in out]
    if rnd.random() < (1 - q) * 0.4:
        out.append(rbox())
    return out


def bbox_solver(truth, q):
    def solve(t):
        bs = noisy(truth[t['input_values']['photo_id']], q)
        return {'boxes': [{'shape': 'rectangle', **b} for b in bs], 'status': 'butterflies' if bs else 'none', 'doubt': rnd.random() < 0.02}
    return solve


def review_solver(truth, q):
    from boxes import same, clean
    def solve(t):
        iv = t['input_values']
        if t.get('known_solutions'):
            right = t['known_solutions'][0]['output_values']['verdict']
        else:
            right = 'OK' if same(truth[iv['photo_id']], clean(iv['boxes'])) is not False else 'BAD'
        v = right if rnd.random() < q else ('BAD' if right == 'OK' else 'OK')
        return {'verdict': v, 'errors': {'missed': True} if v == 'BAD' else {}}
    return solve


def main():
    tmp = tempfile.mkdtemp(prefix='bfly_')
    for x in ['boxes.py', 'prep.py', 'api.py', 'build_gold.py', 'finalize.py', 'gold_tool.template.html']:
        shutil.copy(os.path.join(HERE, x), tmp)
    shutil.copytree(os.path.join(HERE, 'yt'), os.path.join(tmp, 'yt'))
    os.makedirs(os.path.join(tmp, 'data'))
    meta = next(p for p in [os.path.join(HERE, '../dataset/metadata.csv'), os.path.join(HERE, 'data/metadata.csv')] if os.path.exists(p))
    shutil.copy(meta, os.path.join(tmp, 'data/metadata.csv'))
    os.makedirs(os.path.join(tmp, 'work'))
    shutil.copy(os.path.join(HERE, 'work/candidates.json'), os.path.join(tmp, 'work'))
    os.chdir(tmp); sys.path.insert(0, tmp)
    os.environ['YT_TOKEN'] = 'fake'
    import prep, build_gold, api, finalize
    finalize.OUT = 'golden_out'
    from boxes import score, clean
    prep.cmd_photos()

    # --- эталон: два «разметчика» со случайными рамками (фикстура) ---
    cands = json.load(open('work/candidates.json'))[:60]
    tags = ['single', 'multi', 'overlap', 'cropped', 'small', 'similar', 'none']
    a, b = [], []
    for i, c in enumerate(cands):
        bf = [rbox() for _ in range(rnd.choice([0, 1, 1, 2]))]
        ds = [rbox()] if rnd.random() < 0.4 else []
        rec = dict(c, status='ok' if bf else 'none', tags=[tags[i % 7]] if i < 14 else [],
                   boxes=[dict(x, label='butterfly') for x in bf] + [dict(x, label='distractor') for x in ds])
        a.append(dict(rec, annotator='A'))
        jit = [dict(x, left=x['left'] + 0.003) if x['label'] == 'butterfly' else x for x in rec['boxes']]
        if i % 15 == 3:
            jit = jit + [dict(rbox(), label='butterfly')]           # конфликт разметчиков
        b.append(dict(rec, boxes=jit, annotator='B', status='ok' if any(x['label'] == 'butterfly' for x in jit) else 'none'))
    json.dump(a, open('work/gold_A.json', 'w')); json.dump(b, open('work/gold_B.json', 'w'))
    build_gold.render = build_gold.collage = lambda *x, **k: None   # без скачивания картинок
    build_gold.main()
    gold = json.load(open('work/gold.json'))
    assert len(gold) == 60 - 4, len(gold)                             # 4 конфликта (i = 3, 18, 33, 48) не попали в эталон
    for k in ['rev_train', 'rev_exam', 'rev_honey']:
        for it in json.load(open(f'work/{k}.json')):
            from boxes import same
            g = next(x for x in gold if x['photo_id'] == it['photo_id'])
            assert same(g['boxes'], clean(it['boxes'])) is (it['verdict'] == 'OK'), it['id']
    assert '{{EX:' not in open('yt/instruction_bbox_final.html').read()

    # --- платформа ---
    fake = Fake(); api.call = fake
    api.cmd_skills(); api.cmd_projects()
    api.cmd_bbox_exam(); fake.pools[api.state()['pool_bbox_exam']]['status'] = 'OPEN'
    exam_truth = {e['photo_id']: e['boxes'] for e in json.load(open('work/bbox_exam.json'))}
    workers = {f'w{i}': rnd.choice([0.98, 0.95, 0.9, 0.6, 0.3]) for i in range(40)}
    for w, q in list(workers.items())[:25]:
        fake.take(api.state()['pool_bbox_exam'], w, bbox_solver(exam_truth, q))
    api.cmd_score_exam()
    s = api.state()
    passed = [w for w in workers if fake.user_skills.get((w, s['bbox_exam']), 0) >= api.BBOX_PASS]
    good = sum(workers[w] >= 0.9 for w in passed) / len(passed)
    assert all(workers[w] > 0.3 for w in passed) and good >= 0.8, [(w, workers[w]) for w in passed]
    print(f'допущено {len(passed)}, из них сильных (q ≥ 0.9) {good:.0%}')
    # повторно экзамен не дают: навык уже стоит
    assert not fake.take(s['pool_bbox_exam'], passed[0], bbox_solver(exam_truth, 1))

    api.cmd_rev_exam()
    reviewers = {f'r{i}': rnd.choice([0.97, 0.93, 0.85]) for i in range(12)}
    for r in reviewers:                     # навык rev_exam в жизни ставит платформа по экзамену; тут — сразу
        fake.user_skills[(r, s['rev_exam'])] = 90
    dual = passed[-1]                       # сдал bbox и пытается стать ещё и ревьюером
    fake.user_skills[(dual, s['rev_exam'])] = 90
    reviewers[dual] = 1.0
    api.cmd_rev_pool(); api.cmd_bbox_pool('pilot')
    s = api.state()
    for p in s['pools_bbox'] + [s['pool_review']]:
        fake.pools[p]['status'] = 'OPEN'

    truth = {p['photo_id']: ([rbox() for _ in range(rnd.choice([1, 1, 2, 3]))] if p['role'] == 'positive' else [])
             for p in json.load(open('work/photos.json'))}
    for it in range(40):
        for w in passed:
            for p in s['pools_bbox']:
                for _ in range(rnd.randint(5, 30)):
                    fake.take(p, w, bbox_solver(truth, workers[w]))
        api.cmd_cycle()
        for r, q in reviewers.items():
            for _ in range(rnd.randint(5, 40)):
                fake.take(s['pool_review'], r, review_solver(truth, q))
        if it == 2:                              # имитация обрыва: решения на платформе есть, журнал — старый
            snap = open('work/ledger.json').read()
            api.cmd_cycle()
            open('work/ledger.json', 'w').write(snap)
        assert api.autopilot_step() or all(p.get('final') for p in api.ledger()['photos'].values())
        led = api.ledger()
        if all(p.get('final') for p in led['photos'].values()):
            break
    led = api.ledger()
    main_log = [l for l in led['log'] if l[2] == 'batch added']
    assert main_log and main_log[0][1] == 'main', 'автопилот не долил основные фото'
    assert len(s['pools_bbox']) == 1, 'основные фото должны идти в тот же пул'
    print('автопилот:', main_log[0])
    assert len(led['photos']) == 400 and all(p.get('final') for p in led['photos'].values()), 'не все фото дошли до решения'
    # инварианты конвейера
    rev_workers = {a['user_id'] for a in fake.asg.values() if a['pool_id'] == s['pool_review']}
    bbox_workers = {a['user_id'] for a in fake.asg.values() if a['pool_id'] in s['pools_bbox']}
    assert not rev_workers & bbox_workers, 'один человек и размечал, и проверял'
    for aid, a in led['ann'].items():
        st_a = fake.asg[aid]['status']
        if a['decision']:
            assert st_a == ('ACCEPTED' if a['decision'].startswith('accept') else 'REJECTED'), (a['decision'], st_a)
    att = collections.Counter(p['attempts'] for p in led['photos'].values())
    per = collections.Counter(t['input_values']['photo_id'] for t in fake.tasks.values() if t['pool_id'] in s['pools_bbox'])
    assert max(per.values()) <= api.MAX_ATTEMPTS, ('дубли заданий после обрыва', per.most_common(3))
    assert max(att) <= api.MAX_ATTEMPTS, att

    # --- ручной разбор: «команда» ставит истину (фикстура) ---
    finalize.cmd_queue()
    q = json.load(open('work/fix_queue.json'))
    json.dump([{'photo_id': x['photo_id'], 'status': 'ok' if truth[x['photo_id']] else 'none', 'annotator': 'T',
                'comment': 'исправлено', 'boxes': [dict(b, label='butterfly') for b in truth[x['photo_id']]]} for x in q],
              open('work/fix_T.json', 'w'))
    finalize.cmd_build()
    g = [json.loads(l) for l in open('golden_out/golden.jsonl')]
    assert len(g) == 400 and len({x['photo_id'] for x in g}) == 400 and all(isinstance(x['photo_id'], int) and x['error'] is None for x in g)
    assert all(0 <= b[k] <= 1 for x in g for b in x['boxes'] for k in b)
    f1, p, r, *_ = score([(truth[str(x['photo_id'])], x['boxes']) for x in g])
    spent = sum(float(a['reward']) for a in fake.asg.values() if a['status'] == 'ACCEPTED') * (1 + api.FEE)
    print(f'\nOK. golden vs синтетическая истина: F1@0.5 {f1:.3f} (P {p:.3f}, R {r:.3f}); попытки {dict(att)}; '
          f'решения {dict(collections.Counter(a["decision"] for a in led["ann"].values()))}; потрачено (симуляция) {spent:.0f} ₽')
    # аварийный стоп по деньгам: пулы закрываются, cycle останавливается
    api.BUDGET_STOP = 1
    try:
        api.cmd_cycle(); raise AssertionError('стоп по бюджету не сработал')
    except SystemExit:
        pass
    assert all(fake.pools[p]['status'] == 'CLOSED' for p in s['pools_bbox'] + [s['pool_review']]), 'стоп по бюджету не закрыл пулы'
    print('стоп по бюджету: пулы закрыты')
    shutil.rmtree(tmp)


if __name__ == '__main__':
    main()
