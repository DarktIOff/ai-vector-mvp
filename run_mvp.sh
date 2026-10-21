#!/bin/zsh

set -eu
unsetopt BG_NICE

PROJECT_DIR="${0:A:h}"
PYTHON_BIN="${AI_VECTOR_PYTHON:-/Users/Melchior@Magi/Virtual Environment/gen_venv/bin/python}"
BACKEND_HOST="127.0.0.1"
BACKEND_PORT="8000"
FRONTEND_HOST="127.0.0.1"
FRONTEND_PORT="4173"
INSTALL_DEPENDENCIES=false
BACKEND_PID=""
FRONTEND_PID=""
CLEANED_UP=false

usage() {
  print "Использование: ./run_mvp.sh [--install] [--help]"
  print ""
  print "  --install  установить/обновить Python и Node.js зависимости, затем запустить"
  print "  --help     показать эту справку"
  print ""
  print "Python можно переопределить переменной AI_VECTOR_PYTHON."
}

for argument in "$@"; do
  case "$argument" in
    --install)
      INSTALL_DEPENDENCIES=true
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      print -u2 "Неизвестный аргумент: $argument"
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "$PYTHON_BIN" ]]; then
  print -u2 "Python не найден: $PYTHON_BIN"
  print -u2 "Укажите путь через AI_VECTOR_PYTHON=/path/to/python."
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  print -u2 "Команда npm не найдена. Установите Node.js 20 или новее."
  exit 1
fi

cd "$PROJECT_DIR"

if [[ "$INSTALL_DEPENDENCIES" == true ]]; then
  print "Устанавливаю Python-зависимости в:"
  print "  $PYTHON_BIN"
  "$PYTHON_BIN" -m pip install -r "$PROJECT_DIR/requirements.txt"
  print ""
  print "Устанавливаю Node.js-зависимости..."
  npm install
  print ""
fi

if ! "$PYTHON_BIN" -c \
  "import fastapi, sqlalchemy, openpyxl, xlrd, docx, openai" \
  >/dev/null 2>&1; then
  print -u2 "Не все Python-зависимости установлены."
  print -u2 "Запустите: ./run_mvp.sh --install"
  exit 1
fi

if [[ ! -x "$PROJECT_DIR/node_modules/.bin/vite" ]]; then
  print -u2 "Node.js-зависимости не установлены."
  print -u2 "Запустите: ./run_mvp.sh --install"
  exit 1
fi

if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"$BACKEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    print -u2 "Порт $BACKEND_PORT уже занят. Остановите существующий backend."
    exit 1
  fi
  if lsof -nP -iTCP:"$FRONTEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    print -u2 "Порт $FRONTEND_PORT уже занят. Остановите существующий frontend."
    exit 1
  fi
fi

cleanup() {
  if [[ "$CLEANED_UP" == true ]]; then
    return
  fi
  CLEANED_UP=true
  trap - INT TERM HUP EXIT
  print ""
  print "Останавливаю ИИ-ВЕКТОР..."

  if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill -TERM "$FRONTEND_PID" 2>/dev/null || true
  fi
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill -TERM "$BACKEND_PID" 2>/dev/null || true
  fi

  if [[ -n "$FRONTEND_PID" ]]; then
    wait "$FRONTEND_PID" 2>/dev/null || true
  fi
  if [[ -n "$BACKEND_PID" ]]; then
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}

trap 'cleanup; exit 130' INT TERM HUP
trap cleanup EXIT

print "Запускаю ИИ-ВЕКТОР..."
print "  Backend:  http://$BACKEND_HOST:$BACKEND_PORT"
print "  Frontend: http://$FRONTEND_HOST:$FRONTEND_PORT"
print "  Остановка: Ctrl+C"
print ""

PYTHONPATH="$PROJECT_DIR" "$PYTHON_BIN" -m uvicorn \
  backend.app.main:app \
  --host "$BACKEND_HOST" \
  --port "$BACKEND_PORT" &
BACKEND_PID=$!

"$PROJECT_DIR/node_modules/.bin/vite" \
  --host "$FRONTEND_HOST" \
  --port "$FRONTEND_PORT" &
FRONTEND_PID=$!

exit_status=0
while true; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID" || exit_status=$?
    print -u2 "Backend завершился. Останавливаю frontend."
    break
  fi
  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    wait "$FRONTEND_PID" || exit_status=$?
    print -u2 "Frontend завершился. Останавливаю backend."
    break
  fi
  sleep 1
done

exit "$exit_status"
