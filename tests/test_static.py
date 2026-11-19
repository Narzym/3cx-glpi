"""
Testy statyczne: wykrywają typowe skutki NIEUDANEGO MERGE'A bez uruchamiania logiki aplikacji.

Po merge'u z konfliktami łatwo (np. przez "Accept Both Changes" w VS Code) zostawić w jednym pliku
kopię funkcji, która mieszka już w innym module. Taka kopia przesłania właściwą wersję, a zwykle
brakuje jej importów, więc aplikacja wywala się dopiero przy pierwszym połączeniu.
Te testy łapią to od razu i mówią, w którym pliku jest problem.

Uruchomienie: python3 -m unittest discover -s tests -b -v
"""

import ast
import builtins
import dis
import importlib
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Importujemy moduły tylko po to, by sprawdzić nazwy - nic nie łączy się z GLPI ani z bazą produkcyjną.
_TMP = tempfile.mkdtemp(prefix="3cx_static_")
for _key, _value in {
    "GLPI_API_URL": "http://localhost/api.php/v1",
    "GLPI_APP_TOKEN": "x",
    "GLPI_USER_TOKEN": "x",
    "DB_PATH": os.path.join(_TMP, "static.db"),
    "LOG_PATH": os.path.join(_TMP, "static.log"),
}.items():
    os.environ.setdefault(_key, _value)

MODULES = ["utils", "logger", "db", "glpi_client", "glpi_cache", "tickets", "cdr_to_glpi", "admin_panel"]


def top_level_definitions(module_name):
    path = os.path.join(ROOT, f"{module_name}.py")

    with open(path, encoding="utf-8") as file:
        tree = ast.parse(file.read(), filename=path)

    return [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))]


def function_code_objects(code):
    """Wszystkie funkcje (także zagnieżdżone) z danego kodu, bez kodu samego modułu."""

    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield const
            yield from function_code_objects(const)


class TestStaticIntegrity(unittest.TestCase):
    def test_no_function_is_defined_in_two_modules(self):
        owners = {}

        for module in MODULES:
            for name in top_level_definitions(module):
                owners.setdefault(name, []).append(f"{module}.py")

        duplicates = {name: files for name, files in owners.items() if len(files) > 1}

        self.assertEqual(
            duplicates, {},
            "Te funkcje sa zdefiniowane w kilku plikach naraz (typowy skutek 'Accept Both Changes' "
            "przy merge'u). Zostaw jedna definicje we wlasciwym module: " + repr(duplicates)
        )

    def test_no_function_is_defined_twice_in_one_file(self):
        for module in MODULES:
            names = top_level_definitions(module)
            repeated = sorted({name for name in names if names.count(name) > 1})
            self.assertEqual(repeated, [], f"{module}.py: funkcje zdefiniowane wiecej niz raz: {repeated}")

    def test_every_name_used_in_functions_is_defined_or_imported(self):
        problems = []

        for module_name in MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError as error:
                if module_name == "admin_panel":
                    continue  # panel wymaga modulu curses (brak na Windowsie)
                raise error

            path = os.path.join(ROOT, f"{module_name}.py")

            with open(path, encoding="utf-8") as file:
                code = compile(file.read(), path, "exec")

            for function in function_code_objects(code):
                for instruction in dis.get_instructions(function):
                    if instruction.opname != "LOAD_GLOBAL":
                        continue

                    name = instruction.argval

                    if not hasattr(module, name) and not hasattr(builtins, name):
                        problems.append(
                            f"{module_name}.py: funkcja '{function.co_name}' uzywa nazwy '{name}', "
                            f"ktorej ten plik nie definiuje ani nie importuje"
                        )

        self.assertEqual(problems, [], "\n" + "\n".join(sorted(set(problems))))


if __name__ == "__main__":
    unittest.main()
