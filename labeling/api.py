# Все действия в Яндекс Заданиях через API. Токен: export YT_TOKEN=... или файл ~/.yt_token (без токена — dry-run)
# Запуск пулов только с --yes: python api.py open <id> без флага показывает, что и почём откроется
#
# Подготовка (один раз):
#   python api.py balance
#   python api.py skills                       # навыки bbox_exam, rev_exam
#   python api.py projects                     # проекты bbox и review (шаблон, спецификации, инструкции)
# Допуск:
#   python api.py bbox-exam                    # экзамен bbox: 10 эталонных фото, 1 страница
#   python api.py score-exam [--loop 10]       # считаем F1 каждого по эталону → навык bbox_exam (раз в N минут)
#   python api.py rev-exam                     # обучение + экзамен ревью (навык rev_exam ставит сама платформа)
# Разметка:
#   python api.py rev-pool                     # пул ревью + контрольные задания
#   python api.py bbox-pool pilot|main         # пул bbox с отложенной приёмкой + фото
#   python api.py cycle [--loop 10]            # sync + decide: bbox-ответы → ревью → принять/отклонить → переразметка
# Служебное:
#   python api.py open|close|status <pool_id>   python api.py cost
# id всех объектов — в work/state.json, история решений — в work/ledger.json
import json, os, sys, time, datetime, collections, urllib.request
import requests
from boxes import clean, score

API = 'https://tasks.yandex.ru/api/v1'
# токен: переменная YT_TOKEN или файл ~/.yt_token (chmod 600) — чтобы не светить его в командах и чатах
_TF = os.path.expanduser('~/.yt_token')
TOKEN = os.environ.get('YT_TOKEN') or (open(_TF).read().strip() if os.path.exists(_TF) else None)
DRY = not TOKEN or os.environ.get('YT_DRY') == '1'     # YT_DRY=1 — холостой прогон даже при наличии токена
STATE, LEDGER = 'work/state.json', 'work/ledger.json'
FEE = 0.4                      # комиссия платформы (как в ДЗ2); проверить в интерфейсе при создании пула

# --- параметры (обоснование — в README) ---
BBOX_EXAM_PRICE, BBOX_EXAM_PEOPLE = 1.5, 20   # 10 фото ≈ 3–4 мин; волна: 20 × 1.5 × 1.4 = 42 ₽, добавить людей — exam-overlap
EXAM_IOU, BBOX_PASS = 0.5, 80                  # навык = 100·F1@IoU0.5 (как в Контесте) только по фото С бабочками.
# Почему не по всем: на 1-й волне 18 из 20 обвели 3 из 4 дневных молей (по-русски это тоже «бабочки») — экзамен мерил
# не умение рисовать рамки, а энтомологию. Молей отсекает ревью (контрольные «лишняя рамка на моли») + инструкция.
BBOX_PRICE = 0.4                               # за фото, 1 фото на страницу: приёмка/отклонение по каждому фото отдельно
REVIEW_PERIOD_DAYS = 7                         # отложенная приёмка: успеть проверить, иначе примется автоматически
MAX_ATTEMPTS = 2                               # разметка + 1 переразметка; дальше — ручной разбор командой
REV_PRICE, REV_REAL, REV_GOLD = 0.6, 9, 1      # страница ревью: 9 проверок + 1 контрольная, ~1 мин → ~36 ₽/ч
REV_OVERLAP = 3                                # 3 независимых ревьюера: решение большинством, ничьих нет
REV_EXAM_PRICE, REV_EXAM_PEOPLE = 0.5, 20     # 16 заданий ≈ 2 мин; волна: 20 человек
REV_PASS, REV_TRAIN_PASS = 80, 70
BUDGET_NO_REDO, BUDGET_STOP = 700, 780         # ₽ с комиссией: выше — без переразметок; ещё выше — пулы закрываются


def state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}


def save(**kw):
    if DRY:                    # dry-run не должен портить настоящее состояние
        print('[dry] state +', kw); return
    s = state(); s.update(kw); json.dump(s, open(STATE, 'w'), indent=1)


def need(key):
    s = state()
    if key not in s:
        sys.exit(f'нет {key} в {STATE} — сначала выполните предыдущий шаг')
    return s[key]


def _http(method, path, body=None, params=None):
    if DRY:
        print(f'\n[dry] {method} {API}{path} {params or ""}')
        if body is not None:
            print(json.dumps(body, ensure_ascii=False, indent=1)[:2500])
        return {'id': f'DRY_{path.strip("/").replace("/", "_")}', 'items': [], 'has_more': False}
    for attempt in range(3):
        r = requests.request(method, API + path, json=body, params=params,
                             headers={'Authorization': f'OAuth {TOKEN}'}, timeout=180)
        if r.status_code in (429, 502, 503, 504) and attempt < 2:
            time.sleep(5 * (attempt + 1)); continue
        if r.status_code >= 400:
            sys.exit(f'{method} {path} → {r.status_code}\n{r.text[:2000]}')
        return r.json() if r.text else {}


call = _http          # тесты подменяют на фейковый бэкенд


def paged(path, **params):
    out, last = [], None
    while True:
        p = {'limit': 1000, 'sort': 'id', **params}
        if last:
            p['id_gt'] = last
        r = call('GET', path, params=p)
        out += r['items']
        if not r.get('has_more') or not r['items']:
            return out
        last = r['items'][-1]['id']


def load(name):
    return json.load(open(f'work/{name}.json'))


# --- фильтры и правила контроля качества ---
lang_ru = {'category': 'profile', 'key': 'languages', 'operator': 'IN', 'value': 'RU'}
no_skill = lambda sid: {'category': 'skill', 'key': sid, 'operator': 'EQ'}     # «навыка нет» = EQ без value
skill_gte = lambda sid, v: {'category': 'skill', 'key': sid, 'operator': 'GTE', 'value': v}
ban = lambda days, why: {'type': 'RESTRICTION_V2', 'parameters': {
    'scope': 'PROJECT', 'duration_unit': 'DAYS', 'duration': days, 'private_comment': why}}


def fast_rule(history, sec, total, fast, days=30):
    return {'collector_config': {'type': 'ASSIGNMENT_SUBMIT_TIME', 'parameters': {'history_size': history, 'fast_submit_threshold_seconds': sec}},
            'rules': [{'conditions': [{'key': 'total_submitted_count', 'operator': 'GTE', 'value': total},
                                      {'key': 'fast_submitted_count', 'operator': 'GTE', 'value': fast}],
                       'action': ban(days, f'fast answers < {sec}s')}]}


def pool_body(project, name, price, real, gold, flt, qc=None, overlap=1, auto_accept=True, max_sec=900, force_delay=300):
    b = {
        'project_id': project, 'private_name': name,
        'may_contain_adult_content': False,
        'will_expire': (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S'),
        'reward_per_assignment': price,
        'assignment_max_duration_seconds': max_sec,
        'auto_accept_solutions': auto_accept,
        'auto_close_after_complete_delay_seconds': 259200,   # не закрываться сразу: переоткрыть можно только через модерацию
        'defaults': {'default_overlap_for_new_task_suites': overlap, 'default_overlap_for_new_tasks': overlap},
        # force_last_assignment: неполную последнюю страницу всё равно выдаём, иначе хвост пула зависнет
        'mixer_config': {'real_tasks_count': real, 'golden_tasks_count': gold, 'training_tasks_count': 0,
                         'min_real_tasks_count': 1 if real else 0, 'force_last_assignment': True,
                         'force_last_assignment_delay_seconds': force_delay},
        'filter': {'and': flt},
    }
    if not auto_accept:
        b['auto_accept_period_day'] = REVIEW_PERIOD_DAYS
    if qc:
        b['quality_control'] = qc
    return b


def post_tasks(rows):
    # пачками по 100: большие синхронные запросы ловили 504 в ДЗ2
    for i in range(0, len(rows), 100):
        call('POST', '/tasks', rows[i:i + 100], params={'allow_defaults': 'true', 'skip_invalid_items': 'false'})
    return len(rows)


def existing(pool_id, key='photo_id'):
    return {t['input_values'][key] for t in paged('/tasks', pool_id=pool_id)}


def ensure_open(pool_id):
    # переоткрываем только пул, который закрылся сам (всё выполнено); закрытый вручную не трогаем
    if DRY:
        return
    p = call('GET', f'/pools/{pool_id}')
    if p.get('status') != 'OPEN' and p.get('last_close_reason') in ('COMPLETED', 'ALL_TASKS_COMPLETED', None):
        call('POST', f'/pools/{pool_id}/open')
    elif p.get('status') != 'OPEN':
        print(f'! пул {pool_id} закрыт вручную ({p.get("last_close_reason")}) — новые задания ждут, пока вы его не откроете')


# ================= подготовка =================
def cmd_balance():
    print(call('GET', '/requester'))


def cmd_skills():
    ids = {n: call('POST', '/skills', {'name': n, 'hidden': True, 'private_comment': 'butterfly final project'})['id']
           for n in ['bbox_exam', 'rev_exam']}
    save(**ids); print(ids)


def view_spec(path):
    cfg = open(path, encoding='utf-8').read()
    import re
    comps = sorted({t for t in re.findall(r'"type":\s*"([a-z]+\.[a-z0-9.\-]+)"', cfg) if not t.startswith('data.')})
    return {'type': 'tb', 'config': cfg, 'lock': {'core': '1.0.0', **{c: '1.0.0' for c in comps}}}


def cmd_projects():
    specs = json.load(open('yt/specs.json'))
    meta = {
        'bbox': ('Выделите бабочек на фото', 'Обведите рамкой каждую взрослую бабочку на фотографии природы', 'yt/bbox_template.json'),
        'review': ('Проверьте, верно ли выделены бабочки', 'Посмотрите на фото с рамками и решите, верно ли выделены бабочки', 'yt/review_template.json'),
    }
    for kind, (name, descr, tpl) in meta.items():
        instr = f'yt/instruction_{kind}_final.html'
        if not os.path.exists(instr):
            sys.exit(f'нет {instr} — сначала python build_gold.py')
        p = call('POST', '/projects', {
            'public_name': name, 'public_description': descr,
            'public_instructions': open(instr, encoding='utf-8').read(),
            'private_comment': f'butterfly final project: {kind}',
            'assignments_issuing_type': 'AUTOMATED',
            'task_spec': {'input_spec': specs[kind]['input'], 'output_spec': specs[kind]['output'], 'view_spec': view_spec(tpl)}})
        save(**{f'project_{kind}': p['id']}); print(kind, p['id'])



def cmd_update_instructions():
    # новые инструкции в существующие проекты (после правки yt/instruction_*.html и build_gold.py)
    for kind in ('bbox', 'review'):
        pid = need(f'project_{kind}')
        p = call('GET', f'/projects/{pid}')
        body = {k: v for k, v in p.items() if k not in ('id', 'owner', 'created', 'status')}
        body['public_instructions'] = open(f'yt/instruction_{kind}_final.html', encoding='utf-8').read()
        r = call('PUT', f'/projects/{pid}', body)
        print(kind, pid, 'инструкция обновлена:', len(r.get('public_instructions', '')), 'символов,',
              r.get('public_instructions', '').count('<img'), 'картинок')

# ================= допуск bbox =================
def cmd_bbox_exam():
    s = state()
    # заходят только «чистые»: без навыков bbox_exam/rev_exam → один человек = одна попытка и одна роль
    flt = [lang_ru, no_skill(need('bbox_exam')), no_skill(need('rev_exam'))]
    qc = {'configs': [fast_rule(5, 60, 1, 1)]}            # 10 фото меньше чем за минуту — не размечал
    exam = load('bbox_exam')
    p = call('POST', '/pools', pool_body(need('project_bbox'), 'bbox exam', BBOX_EXAM_PRICE, len(exam), 0, flt, qc,
                                         overlap=BBOX_EXAM_PEOPLE, max_sec=1500))
    save(pool_bbox_exam=p['id'])
    post_tasks([{'pool_id': p['id'], 'input_values': {'photo_id': e['photo_id'], 'image': e['image']}, 'overlap': BBOX_EXAM_PEOPLE}
                for e in exam])
    print('bbox exam pool', p['id'], '— откройте: python api.py open', p['id'])


def answers(pool_id, statuses=('SUBMITTED', 'ACCEPTED')):
    # плоский список ответов: одна строка = одно фото в одном assignment
    out = []
    for a in paged('/assignments', pool_id=pool_id):
        if a['status'] not in statuses:
            continue
        for t, sol in zip(a['tasks'], a['solutions']):
            out.append({'assignment_id': a['id'], 'worker': a['user_id'], 'status_a': a['status'], 'task_id': t['id'],
                        'input': t['input_values'], 'output': sol['output_values'],
                        'submitted': a.get('submitted'), 'reward': a.get('reward')})
    return out


def bbox_answer(r):
    o = r['output']
    return {'photo_id': r['input']['photo_id'], 'assignment_id': r['assignment_id'], 'worker': r['worker'],
            'status': o.get('status'), 'boxes': clean(o.get('boxes')) if o.get('status') == 'butterflies' else [],
            'doubt': bool(o.get('doubt'))}


def exam_rows(exam, by):
    # навык — F1 по фото с бабочками; ложные рамки на фото без бабочек (моли) считаем отдельно, для анализа
    rows = []
    for w, ans in by.items():
        if len(ans) < len(exam):
            continue
        pos = [a for a in ans if exam[a['photo_id']]]
        f1, p, rc, tp, fp, fn = score([(exam[a['photo_id']], a['boxes']) for a in pos], EXAM_IOU)
        neg_fp = sum(bool(a['boxes']) for a in ans if not exam[a['photo_id']])
        rows.append((w, round(100 * f1), p, rc, tp, fp, fn, neg_fp))
    return rows


def cmd_score_exam(*args):
    # --rescore: перезаписать уже выставленные навыки (после смены правила подсчёта)
    exam = {e['photo_id']: e['boxes'] for e in load('bbox_exam')}
    sid, pool = need('bbox_exam'), need('pool_bbox_exam')
    rescore = '--rescore' in args
    while True:
        try:
            by = collections.defaultdict(list)
            for r in answers(pool):
                by[r['worker']].append(bbox_answer(r))
            scored = {} if rescore else state().get('exam_scored', {})
            rows = exam_rows(exam, by)
            for w, val, *_ in rows:
                if scored.get(w) != val:
                    call('PUT', '/user-skills', {'skill_id': sid, 'user_id': w, 'value': val})
                    scored[w] = val
            rescore = False
            save(exam_scored=scored)
            json.dump([a for ans in by.values() for a in ans], open('work/exam_answers.json', 'w'), ensure_ascii=False, indent=1)
            with open('work/exam_scores.tsv', 'w') as f:
                f.write('worker\tskill\tprecision\trecall\ttp\tfp\tfn\tboxes_on_negatives\n')
                f.writelines(f'{w}\t{v}\t{p:.3f}\t{r:.3f}\t{tp}\t{fp}\t{fn}\t{nf}\n' for w, v, p, r, tp, fp, fn, nf in sorted(rows, key=lambda x: -x[1]))
            passed = sum(v >= BBOX_PASS for v in scored.values())
            print(f'{time.strftime("%H:%M")} экзамен bbox: сдали работу {len(rows)}, допущено (≥{BBOX_PASS}) {passed} → work/exam_scores.tsv')
        except (requests.RequestException, SystemExit) as e:        # обрыв сети не должен ронять фоновую ждалку
            if '--loop' not in args:
                raise
            print(f'{time.strftime("%H:%M")} ошибка, повторю в следующий раз: {str(e)[:200]}')
        if '--loop' not in args:
            return
        time.sleep(60 * int(args[args.index('--loop') + 1]))


# ================= допуск review =================
def golden_task(pool_id, it, hint=False, overlap=None):
    t = {'pool_id': pool_id,
         'input_values': {'photo_id': it['id'], 'image': it['image'], 'boxes': it['boxes'], 'status': it['status'], 'assignment_id': 'gold'},
         'known_solutions': [{'output_values': {'verdict': it['verdict']}, 'correctness_weight': 1}]}
    if hint:
        t['message_on_unknown_solution'] = it['hint']
    if overlap:
        t['overlap'] = overlap
    else:
        t['infinite_overlap'] = True
    return t


def cmd_rev_exam():
    proj, train, exam = need('project_review'), load('rev_train'), load('rev_exam')
    tr = call('POST', '/trainings', {
        'project_id': proj, 'private_name': 'review training', 'may_contain_adult_content': False,
        'assignment_max_duration_seconds': 1200, 'mix_tasks_in_creation_order': False, 'shuffle_tasks_in_task_suite': True,
        'training_tasks_in_task_suite_count': len(train) // 2, 'task_suites_required_to_pass': 2,
        'retry_training_after_days': 1, 'inherited_instructions': True})
    save(training_review=tr['id'])
    post_tasks([golden_task(tr['id'], it, hint=True) for it in train])
    qc = {'training_requirement': {'training_pool_id': tr['id'], 'training_passing_skill_value': REV_TRAIN_PASS},
          'configs': [{'collector_config': {'type': 'GOLDEN_SET', 'parameters': {'history_size': len(exam)}},
                       'rules': [{'conditions': [{'key': 'golden_set_answers_count', 'operator': 'GTE', 'value': len(exam)}],
                                  'action': {'type': 'SET_SKILL_FROM_OUTPUT_FIELD',
                                             'parameters': {'skill_id': need('rev_exam'), 'from_field': 'correct_answers_rate'}}}]}]}
    flt = [lang_ru, no_skill(need('rev_exam')), no_skill(need('bbox_exam'))]
    ex = call('POST', '/pools', pool_body(proj, 'review exam', REV_EXAM_PRICE, 0, len(exam), flt, qc, overlap=REV_EXAM_PEOPLE))
    save(pool_rev_exam=ex['id'])
    post_tasks([golden_task(ex['id'], it, overlap=REV_EXAM_PEOPLE) for it in exam])
    print('training', tr['id'], 'exam', ex['id'], '— откройте: python api.py open-training', tr['id'], '&& python api.py open', ex['id'])


# ================= основные пулы =================
def cmd_rev_pool():
    qc = {'configs': [
        {'collector_config': {'type': 'GOLDEN_SET', 'parameters': {'history_size': 10}},
         'rules': [{'conditions': [{'key': 'golden_set_answers_count', 'operator': 'GTE', 'value': 3},
                                   {'key': 'golden_set_correct_answers_rate', 'operator': 'LT', 'value': 70}],
                    'action': ban(30, 'review honeypots < 70%')}]},
        fast_rule(5, 15, 3, 2),                     # 10 проверок быстрее 15 с — не смотрел
    ]}
    flt = [lang_ru, skill_gte(need('rev_exam'), REV_PASS), no_skill(need('bbox_exam'))]
    # задания на проверку приходят порциями: неполную страницу (9 проверок по цене полной) выдаём только через 30 мин
    p = call('POST', '/pools', pool_body(need('project_review'), 'review main', REV_PRICE, REV_REAL, REV_GOLD, flt, qc,
                                         overlap=REV_OVERLAP, max_sec=1200, force_delay=1800))
    save(pool_review=p['id'])
    post_tasks([golden_task(p['id'], it) for it in load('rev_honey')])
    print('review pool', p['id'], '(задания на проверку добавит cycle)')


def cmd_bbox_pool(batch):
    qc = {'configs': [
        fast_rule(10, 4, 5, 3),                     # фото быстрее 4 с — даже не посмотрел
        {'collector_config': {'type': 'ACCEPTANCE_RATE', 'parameters': {}},     # «результаты отложенной приёмки»
         'rules': [{'conditions': [{'key': 'total_assignments_count', 'operator': 'GTE', 'value': 5},
                                   {'key': 'rejected_assignments_rate', 'operator': 'GT', 'value': 35}],
                    'action': ban(30, 'review rejected > 35%')}]},
    ]}
    flt = [lang_ru, skill_gte(need('bbox_exam'), BBOX_PASS), no_skill(need('rev_exam'))]
    p = call('POST', '/pools', pool_body(need('project_bbox'), f'bbox {batch}', BBOX_PRICE, 1, 0, flt, qc,
                                         overlap=1, auto_accept=False, max_sec=600))
    pools = state().get('pools_bbox', [])
    save(pools_bbox=pools + [p['id']])
    photos = [ph for ph in load('photos') if ph['batch'] == batch]
    post_tasks([{'pool_id': p['id'], 'input_values': {'photo_id': ph['photo_id'], 'image': ph['image']}, 'overlap': 1} for ph in photos])
    led = ledger()
    for ph in photos:
        led['photos'].setdefault(ph['photo_id'], {'attempts': 0, 'tasks': 1, 'pool': p['id'], 'image': ph['image']})
    save_ledger(led)
    print(f'bbox pool {p["id"]}: {len(photos)} фото ({batch}) — откройте: python api.py open {p["id"]}')


# ================= конвейер: bbox → review → решение =================
def ledger():
    return json.load(open(LEDGER)) if os.path.exists(LEDGER) else {'ann': {}, 'photos': {}, 'log': []}


def save_ledger(led):
    if DRY:
        return
    json.dump(led, open(LEDGER, 'w'), ensure_ascii=False, indent=1)


def url_ok(url):
    try:
        req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'ysda-course'})
        return urllib.request.urlopen(req, timeout=30).status == 200
    except Exception:
        return False


def judge(aid, accept, comment=''):
    body = {'status': 'ACCEPTED' if accept else 'REJECTED', 'public_comment': comment or ('Спасибо!' if accept else 'Ошибка в разметке')}
    call('PATCH', f'/assignments/{aid}', body)


def reissue(led, pid, why):
    # новое задание на то же фото в тот же пул bbox (переразметка другим человеком — см. проверку в decide)
    ph = led['photos'][pid]
    post_tasks([{'pool_id': ph['pool'], 'input_values': {'photo_id': pid, 'image': ph['image']}, 'overlap': 1}])
    ph['tasks'] += 1
    ensure_open(ph['pool'])
    led['log'].append([now(), pid, 'reissue', why])


def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def sync(led):
    # новые ответы bbox (ждут приёмки) → задания ревью с перекрытием 3
    rev_pool, new = need('pool_review'), []
    for pool in state().get('pools_bbox', []):
        for r in answers(pool, statuses=('SUBMITTED',)):
            aid = r['assignment_id']
            if aid in led['ann']:
                continue
            a = bbox_answer(r)
            ph = led['photos'][a['photo_id']]
            ph['attempts'] += 1
            a.update(round=ph['attempts'], pool=pool, reviews=[], decision=None)
            led['ann'][aid] = a
            if a['status'] == 'load_error':
                if url_ok(ph['image']):       # картинка открывается → ответ неверный, переразметка без штрафа к попыткам
                    a['decision'] = 'reject_load'
                    judge(aid, False, 'Картинка открывается. Если не видно фото — обновите страницу, не выбирайте этот ответ.')
                    ph['attempts'] -= 1
                    reissue(led, a['photo_id'], 'false load_error')
                else:
                    a['decision'] = 'accept_broken'; judge(aid, True)
                    ph['final'] = 'manual'; led['log'].append([now(), a['photo_id'], 'manual', 'broken image'])
                continue
            new.append({'pool_id': rev_pool, 'overlap': REV_OVERLAP, 'input_values': {
                'photo_id': a['photo_id'], 'image': ph['image'], 'status': a['status'], 'assignment_id': aid,
                'boxes': [{'shape': 'rectangle', **b} for b in a['boxes']]}})
    if new:
        post_tasks(new)
        ensure_open(rev_pool)
    return len(new)


def verdict_errors(o):
    e = o.get('errors') or {}
    return sorted(k for k, v in e.items() if v) if isinstance(e, dict) else sorted(e)


def decide(led, allow_redo=True):
    # ≥2 из 3 «OK» → принять; иначе отклонить и переразметить (до MAX_ATTEMPTS), потом — ручной разбор
    for r in answers(need('pool_review')):
        aid = r['input'].get('assignment_id')
        if aid == 'gold' or aid not in led['ann']:
            continue
        a = led['ann'][aid]
        if any(x['review_assignment'] == r['assignment_id'] for x in a['reviews']):
            continue
        a['reviews'].append({'worker': r['worker'], 'verdict': r['output'].get('verdict'), 'errors': verdict_errors(r['output']),
                             'review_assignment': r['assignment_id'], 'self': r['worker'] == a['worker']})
    done = collections.Counter()
    for aid, a in led['ann'].items():
        if a['decision']:
            continue
        valid = [x for x in a['reviews'] if not x['self']]      # автор не проверяет сам себя
        if len(valid) < REV_OVERLAP:
            continue
        ok = sum(x['verdict'] == 'OK' for x in valid[:REV_OVERLAP])
        ph = led['photos'][a['photo_id']]
        if ok * 2 > REV_OVERLAP:
            a['decision'] = 'accept' if ok == REV_OVERLAP else 'accept_majority'
            judge(aid, True)
            ph['final'] = 'manual' if a['doubt'] else aid              # «не уверен» → команда смотрит отдельно
            if a['doubt']:
                ph['accepted'] = aid
                led['log'].append([now(), a['photo_id'], 'manual', 'annotator doubt'])
        else:
            errs = collections.Counter(e for x in valid for e in x['errors'])
            why = {'missed': 'пропущена бабочка', 'extra': 'лишняя рамка', 'loose': 'неточная рамка'}
            comment = 'Ревьюеры нашли ошибку: ' + ', '.join(why[e] for e, _ in errs.most_common() if e in why) if errs else 'Ошибка в разметке'
            a['decision'] = 'reject'
            judge(aid, False, comment)
            if ph['attempts'] < MAX_ATTEMPTS and allow_redo:
                reissue(led, a['photo_id'], f'rejected {ok}/{REV_OVERLAP} OK')
            else:
                ph['final'] = 'manual'
                led['log'].append([now(), a['photo_id'], 'manual', f'{ph["attempts"]} attempts rejected' if allow_redo else 'budget cap: no re-annotation'])
        done[a['decision']] += 1
    return done


def spent():
    # потрачено с комиссией: принятые + ещё не принятые (их могут принять автоматически) во всех пулах
    s, total = state(), 0.0
    for pid in [s.get('pool_bbox_exam'), s.get('pool_rev_exam'), s.get('pool_review')] + s.get('pools_bbox', []):
        if pid:
            total += sum(float(a.get('reward') or 0) for a in paged('/assignments', pool_id=pid) if a['status'] in ('ACCEPTED', 'SUBMITTED'))
    return total * (1 + FEE)


def add_photos(pool_id, batch):
    # фото батча → задания в уже открытый пул bbox (новый пул пришлось бы снова отправлять на модерацию)
    photos = [ph for ph in load('photos') if ph['batch'] == batch]
    have = existing(pool_id)
    rows = [ph for ph in photos if ph['photo_id'] not in have]
    post_tasks([{'pool_id': pool_id, 'input_values': {'photo_id': ph['photo_id'], 'image': ph['image']}, 'overlap': 1} for ph in rows])
    led = ledger()
    for ph in photos:
        led['photos'].setdefault(ph['photo_id'], {'attempts': 0, 'tasks': 1, 'pool': pool_id, 'image': ph['image']})
    led['log'].append([now(), batch, 'batch added', f'{len(rows)} photos -> pool {pool_id}'])
    save_ledger(led)
    ensure_open(pool_id)
    return len(rows)


def cmd_bbox_add(batch, pool_id=None):
    print(f'добавлено {add_photos(pool_id or state()["pools_bbox"][0], batch)} фото ({batch})')


PILOT_MIN_DECIDED, PILOT_MIN_ACCEPT, PILOT_MAX_MANUAL = 0.9, 0.5, 0.25


def pilot_gate(led):
    # пилот окончен, если решение есть по ≥90% его фото; качество: принято ≥50% первых разметок, на ручной ≤25% фото
    pilot = [ph['photo_id'] for ph in load('photos') if ph['batch'] == 'pilot']
    fin = [p for p in pilot if led['photos'].get(p, {}).get('final')]
    first = [a for a in led['ann'].values() if a['photo_id'] in set(pilot) and a['round'] == 1 and a['decision']]
    acc = sum(a['decision'].startswith('accept') for a in first) / max(1, len(first))
    manual = sum(led['photos'][p].get('final') == 'manual' for p in fin) / max(1, len(pilot))
    done = len(fin) >= PILOT_MIN_DECIDED * len(pilot)
    ok = acc >= PILOT_MIN_ACCEPT and manual <= PILOT_MAX_MANUAL
    return done, ok, f'пилот: решено {len(fin)}/{len(pilot)}, принято с 1-й попытки {acc:.0%}, на ручной {manual:.0%}'


def autopilot_step():
    # один проход: cycle; если пилот окончен и качество норм — доливаем основные фото. Возвращает False, когда пора остановиться
    cmd_cycle()
    led = ledger()
    if not any(l[2] == 'batch added' and l[1] == 'main' for l in led['log']):
        done, ok, msg = pilot_gate(led)
        print(f'{time.strftime("%H:%M")} {msg}')
        if done and ok:
            n = add_photos(state()['pools_bbox'][0], 'main')
            print(f'{time.strftime("%H:%M")} >>> пилот ок, добавлено {n} основных фото в пул {state()["pools_bbox"][0]}')
        elif done:
            print(f'{time.strftime("%H:%M")} !!! пилот закончен, но качество ниже порога — основные фото НЕ добавлены, жду человека')
            return False
    elif all(p.get('final') for p in ledger()['photos'].values()):
        print(f'{time.strftime("%H:%M")} все фото решены — автопилот остановлен')
        return False
    return True


def cmd_autopilot(minutes='5'):
    # ждалка: cycle каждые N минут, после пилота — основные 368 фото в тот же пул; стоп по бюджету роняет её целиком
    while True:
        try:
            if not autopilot_step():
                return
        except requests.RequestException as e:          # обрыв сети — не повод останавливать ждалку
            print(f'{time.strftime("%H:%M")} сеть: {str(e)[:200]} — повторю')
        except SystemExit as e:
            if str(e) == 'budget stop':
                raise
            print(f'{time.strftime("%H:%M")} ошибка API: {str(e)[:300]} — повторю')
        sys.stdout.flush()
        time.sleep(60 * int(minutes))


def cmd_cycle(*args):
    while True:
        money = spent()
        if money > BUDGET_STOP:            # аварийный стоп: закрыть пулы (обратимо), дальше решает человек
            s = state()
            for pid in s.get('pools_bbox', []) + [s.get('pool_review')]:
                call('POST', f'/pools/{pid}/close')
            print(f'!!! потрачено {money:.0f} ₽ > {BUDGET_STOP}: пулы bbox и ревью ЗАКРЫТЫ')
            raise SystemExit('budget stop')
        led = ledger()
        n = sync(led); save_ledger(led)
        d = decide(led, allow_redo=money < BUDGET_NO_REDO); save_ledger(led)
        if money >= BUDGET_NO_REDO:
            print(f'! потрачено {money:.0f} ₽ ≥ {BUDGET_NO_REDO}: переразметки отключены, отклонённое — на ручной разбор')
        ph = led['photos'].values()
        fin = collections.Counter('manual' if p.get('final') == 'manual' else 'accepted' if p.get('final') else 'in work' for p in ph)
        print(f'{time.strftime("%H:%M")} в ревью +{n}; решения {dict(d)}; фото: {dict(fin)} из {len(led["photos"])}; потрачено ≈{money:.0f} ₽')
        if '--loop' not in args or all(p.get('final') for p in ph):
            return
        time.sleep(60 * int(args[args.index('--loop') + 1]))


# ================= служебное =================
def cmd_open(pool_id, *args):
    # запуск пула необратим (модерация, траты): без --yes только показываем, что и почём откроется
    p = call('GET', f'/pools/{pool_id}')
    tasks = paged('/tasks', pool_id=pool_id)
    real = [t for t in tasks if not t.get('known_solutions')]
    mix = p.get('mixer_config', {})
    per_page = max(1, mix.get('real_tasks_count') or 1)
    pages = sum(t.get('overlap', 1) for t in real) / per_page if mix.get('real_tasks_count') else max((t.get('overlap', 1) for t in tasks), default=0)
    print(f"пул {pool_id} «{p.get('private_name')}», статус {p.get('status')}")
    print(f"  заданий {len(tasks)} (контрольных {len(tasks) - len(real)}), на странице {mix.get('real_tasks_count')}+{mix.get('golden_tasks_count')}, "
          f"цена {p.get('reward_per_assignment')} ₽, автоприёмка {p.get('auto_accept_solutions')}")
    print(f"  фильтр: {json.dumps(p.get('filter'), ensure_ascii=False)}")
    print(f"  максимум страниц ≈ {pages:.0f} → до {pages * float(p.get('reward_per_assignment', 0)) * (1 + FEE):.0f} ₽ с комиссией")
    if '--yes' not in args:
        print('НЕ запущен. Чтобы запустить: python api.py open', pool_id, '--yes'); return
    print(call('POST', f'/pools/{pool_id}/open'))


def cmd_close(pool_id):
    print(call('POST', f'/pools/{pool_id}/close'))


def cmd_open_training(tid, *args):
    # обучение бесплатное, но тоже уходит исполнителям — тот же предохранитель
    if '--yes' not in args:
        print('обучение', tid, 'НЕ запущено. Чтобы запустить: python api.py open-training', tid, '--yes'); return
    print(call('POST', f'/trainings/{tid}/open'))


def cmd_status(pool_id):
    p = call('GET', f'/pools/{pool_id}')
    print({k: p.get(k) for k in ['id', 'private_name', 'status', 'last_close_reason']})


def cmd_cost():
    # фактические расходы: принятые задания × цена × (1 + комиссия); отклонённые не оплачиваются, обучение бесплатно
    s, total, rows = state(), 0.0, []
    named = [('экзамен bbox', s.get('pool_bbox_exam')), ('экзамен ревью', s.get('pool_rev_exam')), ('ревью', s.get('pool_review'))]
    named += [(f'разметка bbox {i + 1}', p) for i, p in enumerate(s.get('pools_bbox', []))]
    for name, pid in named:
        if not pid:
            continue
        acc = [a for a in paged('/assignments', pool_id=pid) if a['status'] == 'ACCEPTED']
        pend = [a for a in paged('/assignments', pool_id=pid) if a['status'] == 'SUBMITTED']
        money = sum(float(a.get('reward', 0)) for a in acc) * (1 + FEE)
        total += money
        rows.append(f'{name:18} {pid}: принято {len(acc):4}, ждут решения {len(pend):3}, {money:7.1f} ₽')
    print('\n'.join(rows)); print(f'итого {total:.1f} ₽ из 850')
    json.dump({'rows': rows, 'total': total, 'at': now()}, open('work/cost.json', 'w'), ensure_ascii=False, indent=1)


def cmd_budget():
    # смета без обращения к API: по реальному числу заданий в work/ и параметрам выше
    photos, exam, rexam = load('photos'), load('bbox_exam'), load('rev_exam')
    k, n = 1 + FEE, len(photos)
    redo = 0.15                                        # доля переразметок (в симуляции 7–9%, берём с запасом)
    ann_exp, ann_max = n * (1 + redo), n * MAX_ATTEMPTS
    rows = [
        ('экзамен bbox', f'{BBOX_EXAM_PEOPLE} чел. × {BBOX_EXAM_PRICE} ₽ ({len(exam)} фото)', BBOX_EXAM_PEOPLE * BBOX_EXAM_PRICE * k, BBOX_EXAM_PEOPLE * BBOX_EXAM_PRICE * k),
        ('обучение ревью', 'бесплатно', 0, 0),
        ('экзамен ревью', f'{REV_EXAM_PEOPLE} чел. × {REV_EXAM_PRICE} ₽ ({len(rexam)} заданий)', REV_EXAM_PEOPLE * REV_EXAM_PRICE * k, REV_EXAM_PEOPLE * REV_EXAM_PRICE * k),
        # платим только за принятые: по 1 на фото; максимум — если всё примется автоматически по истечении срока
        ('разметка bbox', f'{n} фото × {BBOX_PRICE} ₽; макс.: все {MAX_ATTEMPTS} попытки оплачены', n * BBOX_PRICE * k, ann_max * BBOX_PRICE * k),
        # ожидаемо: полные страницы; максимум: все попытки + каждая 3-я страница неполная (вдвое дороже на проверку)
        ('ревью', f'{REV_OVERLAP} × разметки / {REV_REAL} на стр. × {REV_PRICE} ₽',
         ann_exp * REV_OVERLAP / REV_REAL * REV_PRICE * k, ann_max * REV_OVERLAP / REV_REAL * REV_PRICE * k * 1.5),
    ]
    print(f'{"этап":15} {"расчёт":55} {"ожидаемо":>9} {"максимум":>9}')
    for name, how, e, m in rows:
        print(f'{name:15} {how:55} {e:8.0f} ₽ {m:8.0f} ₽')
    e, m = sum(r[2] for r in rows), sum(r[3] for r in rows)
    print(f'{"итого":71} {e:8.0f} ₽ {m:8.0f} ₽   (бюджет 850, комиссия {FEE:.0%})')
    print(f'предохранитель в cycle: ≥{BUDGET_NO_REDO} ₽ — без переразметок, >{BUDGET_STOP} ₽ — пулы закрываются; '
          f'реальный потолок ≈ {BUDGET_STOP} ₽ + задания, уже взятые в работу (единицы рублей)')


def cmd_exam_overlap(pool_id, n):
    # сколько человек пройдут экзамен: запускаем волнами, чтобы сдавшие сразу получали работу, а не ждали
    n = int(n)
    for t in paged('/tasks', pool_id=pool_id):
        call('PATCH', f'/tasks/{t["id"]}', {'overlap': n, 'infinite_overlap': False})
    got = {t.get('overlap') for t in paged('/tasks', pool_id=pool_id)}
    print(f'пул {pool_id}: перекрытие заданий теперь {got}')


if __name__ == '__main__':
    if DRY:
        print('YT_TOKEN не задан → dry-run')
    cmd, *args = sys.argv[1:] or ['help']
    fn = globals().get('cmd_' + cmd.replace('-', '_'))
    if not fn:
        sys.exit(open(__file__).read().split('import json')[0])
    fn(*args)
