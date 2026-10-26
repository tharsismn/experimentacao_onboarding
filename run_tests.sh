#!/usr/bin/env bash
# Testes locais (sem workspace). Uso: ./run_tests.sh [Classe[.teste]]
cd "$(dirname "$0")"
if [[ -n "${1:-}" ]]; then python3 -m unittest -v "tests.test_lib.$1"; else python3 -m unittest discover -v -s tests; fi
