# Opdatering til q-insubiz fejlscreenshots

Erstat de komplette filer:

- `main.py`
- `behandel.py`

`pyproject.toml` er medtaget komplet og peger fortsat på `q-insubiz` branch
`main`. Efter at den nye q-insubiz-commit er pushed, skal låsefilen opdateres.

## Hvad ændres

- `PlaywrightRunRecorder` oprettes i process-mode både med og uden `--debug`.
- Recorderen sendes fra `main.py` til `behandel_page()`.
- `behandel.py` sender recorderen til alle q-insubiz UI-funktioner.
- q-insubiz kan derfor kalde `screenshot(..., always=True)` ved UI-fejl.
- Screenshots gemmes lokalt og forsøges uploadet til SharePoint af recorderen.
- Queue-mode er uændret.

## Opdater dependency

```bash
uv lock --upgrade-package q-insubiz
uv sync
```

## Kontrol

```bash
uv run python -c "import inspect; from q_insubiz.functionality.skader import send_digital_post; print(inspect.signature(send_digital_post))"

uvx ruff check behandel.py main.py
uvx ruff format --check behandel.py main.py

uv run python -m py_compile \
    behandel.py \
    main.py

uv run python -c "import behandel; import main; print('Procesimports OK')"
```

Den installerede `send_digital_post`-signatur skal indeholde
`recorder: PlaywrightRunRecorder | None = None`.
