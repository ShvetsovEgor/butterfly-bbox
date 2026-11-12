# Геометрия рамок: IoU, сопоставление как в Контесте (максимальное паросочетание при IoU ≥ t), F1.
# Рамка — dict {left, top, width, height} в долях кадра [0, 1], как отдаёт field.image-annotation и как ждёт Контест.
MIN_SIDE = 0.003   # рамка уже 0.3% кадра — случайный клик, не объект


def clean(raw):
    # ответ field.image-annotation → список рамок; точки/полигоны и вырожденные рамки выкидываем
    out = []
    for s in raw or []:
        if s.get('shape', 'rectangle') != 'rectangle' or s.get('label', 'butterfly') != 'butterfly':
            continue
        l, t, w, h = (float(s[k]) for k in ('left', 'top', 'width', 'height'))
        if w < 0:
            l, w = l + w, -w
        if h < 0:
            t, h = t + h, -h
        l2, t2 = min(1.0, l + w), min(1.0, t + h)
        l, t = max(0.0, l), max(0.0, t)
        if l2 - l >= MIN_SIDE and t2 - t >= MIN_SIDE:
            out.append({'left': round(l, 5), 'top': round(t, 5), 'width': round(l2 - l, 5), 'height': round(t2 - t, 5)})
    return out


def iou(a, b):
    ix = max(0.0, min(a['left'] + a['width'], b['left'] + b['width']) - max(a['left'], b['left']))
    iy = max(0.0, min(a['top'] + a['height'], b['top'] + b['height']) - max(a['top'], b['top']))
    inter = ix * iy
    union = a['width'] * a['height'] + b['width'] * b['height'] - inter
    return inter / union if union > 0 else 0.0


def match(gt, pred, thr=0.5):
    # максимальное число пар gt↔pred с IoU ≥ thr (алгоритм Куна); возвращает [(i_gt, j_pred, iou)]
    adj = [[j for j, p in enumerate(pred) if iou(g, p) >= thr] for g in gt]
    owner = {}

    def try_kuhn(i, seen):
        for j in adj[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or try_kuhn(owner[j], seen):
                owner[j] = i
                return True
        return False

    for i in range(len(gt)):
        try_kuhn(i, set())
    return sorted((i, j, iou(gt[i], pred[j])) for j, i in owner.items())


def counts(gt, pred, thr=0.5):
    tp = len(match(gt, pred, thr))
    return tp, len(pred) - tp, len(gt) - tp


def f1(tp, fp, fn):
    return 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 1.0   # пустой эталон и пустой ответ — идеально


def score(pairs, thr=0.5):
    # pairs: [(gt_boxes, pred_boxes)] → (F1, precision, recall, tp, fp, fn), суммируя по всем фото
    tp = fp = fn = 0
    for gt, pred in pairs:
        a, b, c = counts(gt, pred, thr)
        tp, fp, fn = tp + a, fp + b, fn + c
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return f1(tp, fp, fn), p, r, tp, fp, fn


def same(gt, pred, ok_iou=0.75):
    # вердикт для контрольного задания ревью: True — разметка верна, False — явно неверна, None — пограничная
    m = match(gt, pred, 0.5)
    if len(m) != len(gt) or len(m) != len(pred):
        return False                          # пропуск, лишняя рамка или рамка с IoU < 0.5
    if all(x >= ok_iou for *_, x in m):
        return True
    return None                               # IoU 0.5–0.75: спорно, в контрольные не берём
