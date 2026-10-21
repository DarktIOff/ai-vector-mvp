#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="$PROJECT_ROOT/handoff"
INCLUDE_DATA=0

usage() {
  printf '%s\n' \
    "Использование: ./scripts/make_handoff.sh [--include-data] [--output DIR]" \
    "" \
    "По умолчанию создаётся безопасный архив без .env, rd/, var/," \
    "node_modules, .venv и dist." \
    "" \
    "--include-data  включить rd/; используйте только если передача данных разрешена" \
    "--output DIR    каталог для готового архива"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-data)
      INCLUDE_DATA=1
      shift
      ;;
    --output)
      if [[ $# -lt 2 || -z "$2" ]]; then
        usage
        exit 2
      fi
      OUTPUT_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Неизвестный аргумент: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ai-vector-handoff.XXXXXX")"
trap 'rm -rf "$BUILD_ROOT"' EXIT

PACKAGE_NAME="ai-vector-mvp"
STAGE="$BUILD_ROOT/$PACKAGE_NAME"
mkdir -p "$STAGE"

ROOT_FILES=(
  ".env.example"
  ".gitignore"
  "DEVELOPER_HANDOFF.md"
  "FRONTEND_BRIEF_ИИ-ВЕКТОР.md"
  "README.md"
  "REAL_MVP_IMPLEMENTATION_BRIEF_ИИ-ВЕКТОР.md"
  "analytic-certification"
  "index.html"
  "package-lock.json"
  "package.json"
  "pytest.ini"
  "requirements.txt"
  "run_mvp.sh"
  "tsconfig.app.json"
  "tsconfig.json"
  "tsconfig.node.json"
  "vite.config.ts"
)

for relative_path in "${ROOT_FILES[@]}"; do
  if [[ -f "$PROJECT_ROOT/$relative_path" ]]; then
    cp -p "$PROJECT_ROOT/$relative_path" "$STAGE/$relative_path"
  fi
done

for relative_dir in backend public scripts src; do
  rsync -a \
    --exclude '.DS_Store' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache/' \
    "$PROJECT_ROOT/$relative_dir/" "$STAGE/$relative_dir/"
done

if [[ "$INCLUDE_DATA" -eq 1 ]]; then
  printf '%s\n' \
    "ВНИМАНИЕ: архив будет содержать исходные файлы rd/." \
    "Убедитесь, что их передача разработчикам разрешена."
  rsync -a \
    --exclude '.DS_Store' \
    --exclude '~$*' \
    "$PROJECT_ROOT/rd/" "$STAGE/rd/"
fi

if [[ -e "$STAGE/.env" || -d "$STAGE/var" ]]; then
  printf '%s\n' "Проверка безопасности не пройдена: найден .env или var/." >&2
  exit 1
fi

if find "$STAGE" -type f -name '~$*' -print -quit | grep -q .; then
  printf '%s\n' "Проверка безопасности не пройдена: найден временный файл Office." >&2
  exit 1
fi

STAMP="$(date '+%Y%m%d-%H%M%S')"
SUFFIX="code-only"
if [[ "$INCLUDE_DATA" -eq 1 ]]; then
  SUFFIX="with-data"
fi
ARCHIVE="$OUTPUT_DIR/${PACKAGE_NAME}-${SUFFIX}-${STAMP}.tar.gz"

tar -C "$BUILD_ROOT" -czf "$ARCHIVE" "$PACKAGE_NAME"
CHECKSUM="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
CHECKSUM_FILE="$ARCHIVE.sha256"
printf '%s  %s\n' "$CHECKSUM" "$(basename "$ARCHIVE")" > "$CHECKSUM_FILE"

printf '\nГотово:\n  %s\n  %s\nSHA-256:\n  %s\n' \
  "$ARCHIVE" "$CHECKSUM_FILE" "$CHECKSUM"
printf '%s\n' \
  "" \
  "Перед отправкой рекомендуется распаковать архив и проверить:" \
  "  tar -tzf \"$ARCHIVE\" | less"
