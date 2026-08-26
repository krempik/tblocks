# T-Blocks v2.0

Продвинутый Tetris++ с боссами, powerups, достижениями и лидербордом.

## Игрок

Открой `index.html` в браузере. Всё на одном файле — никаких зависимостей.

### Режимы
- **Classic** — стандартный Тетрис
- **Mirror** — фигуры отражаются
- **Advanced** — двойные линии, комбо
- **Retro** — упрощённый режим

### Боссы
- **CHUNK** — управляет блоками, прыгает по полю
- **SPIKE** — телепортируется, оставляет шипы
- **BOMB** — метает бомбы, взрывает линии

### Powerups (8 штук)
Замедление, взрыв, лазер, телепорт, заморозка, шинковщик, удвоение, зеркало.

### Достижения
12 уникальных достижений.

## Сервер (лидерборд)

```bash
pip install -r requirements.txt
python server.py
```

Сервер запускается на `http://localhost:8001`.

### API
- `POST /api/score` — отправить счёт
- `GET /api/leaderboard?mode=classic` — таблица лидеров
- `GET /api/stats` — статистика

### Cloudflared Tunnel
Сервер автоматически запускает Cloudflare Quick Tunnel (если `cloudflared` установлен).

## Стек

- **Игра:** HTML5 Canvas, Web Audio API, Vanilla JS (~1400 строк)
- **Сервер:** Python, FastAPI, SQLAlchemy, SQLite

## Лицензия

MIT
