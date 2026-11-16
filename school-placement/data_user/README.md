# Данные приложения

Файлы в этой папке подготовлены скриптом `tools/prepare_data.py` из проекта практики 2.
Исходные файлы не изменялись.

- `blocks.json` — кварталы, население, существующая вместимость школ, сохранённая
  обеспеченность и SVG-контуры для схемы.
- `accessibility_matrix_walk.parquet` — матрица времени в пути пешком, минуты.
- `metadata.json` — источники, контрольные суммы, параметры и проверка матрицы.
- `source/` — копии исходных результатов практики 2 для сверки: сводный слой,
  `provision_report.csv` и связи «квартал → школа» (`provision_links_school.parquet`).

Источник: `C:\Users\harlekiro\python-package-boilerplate\school-placement\sources\user_vysotsk`.
Подготовлено: 2026-11-02T22:06:50+00:00.
