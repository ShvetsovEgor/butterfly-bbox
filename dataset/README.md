# Датасет

400 фотографий с [iNaturalist](https://www.inaturalist.org/), лицензия каждой фотографии — **CC0**. Файлы лежат в `images/`, таблица источников — в `metadata.csv`.

`group` и `role` заданы при сборе и не являются разметкой рамок. `positive` — бакет, где ожидали взрослую бабочку. `negative` — бакет, где бабочку размечать не должны: моль, гусеница, куколка, другое насекомое, цветок или гриб. Таксон и стадия iNaturalist не заменяют просмотр кадра.

## Состав

| Группа | Роль | Фото |
| --- | --- | ---: |
| nymphalidae | positive | 52 |
| lycaenidae | positive | 46 |
| hesperiidae | positive | 45 |
| pieridae | positive | 39 |
| papilionidae | positive | 37 |
| riodinidae | positive | 35 |
| larva | negative | 22 |
| flower | negative | 14 |
| moth_erebidae | negative | 13 |
| moth_geometridae | negative | 13 |
| pupa | negative | 13 |
| dragonfly | negative | 12 |
| moth_saturniidae | negative | 12 |
| moth_sphingidae | negative | 12 |
| hoverfly | negative | 10 |
| fungus | negative | 9 |
| bee | negative | 8 |
| ladybird | negative | 8 |

Всего 254 positive и 146 negative.

## Колонки `metadata.csv`

- `photo_id`, `observation_id` — идентификаторы фотографии и наблюдения.
- `group`, `role` — бакет сбора и ожидаемая роль.
- `taxon_id`, `taxon_name`, `taxon_rank`, `common_name` — таксон наблюдения.
- `license_code`, `attribution`, `observer_login` — лицензия и авторство.
- `observation_url`, `original_url`, `large_url` — ссылки на наблюдение и файлы iNaturalist.
- `local_path` — путь внутри этой папки, например `images/758718188.jpg`.
- `bytes`, `variant` — размер скачанного файла и вариант (`large`).
- `filter_level` — насколько был ослаблен фильтр запроса.
- `observed_on`, `place_guess` — дата и место из наблюдения.
