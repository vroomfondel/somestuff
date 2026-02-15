import os
import sys
import sysconfig
import time

print(f"Python Version: {sys.version}")


def info_test() -> None:
    # 1. Prüfen ob GIL aktiv ist
    # Ab Python 3.13 gibt es _is_gil_enabled()
    try:
        gil_status = sys._is_gil_enabled()
        status_text = "AN (Standard)" if gil_status else "AUS (Free-Threaded)"
        print(f"GIL Status:     {status_text}")
    except AttributeError:
        print("GIL Status:     AN (Alte Version oder Standard Build)")

    #  2. Build Tag anzeigen
    jit_connected = sysconfig.get_config_vars().get("Py_TAG")
    print(f"Build Tag:      {jit_connected}")

    # 3. Check ob JIT im Build kompiliert wurde (nicht ob aktiv!)
    config_args = sysconfig.get_config_vars().get("CONFIG_ARGS") or ""
    has_jit_build = "--enable-experimental-jit" in config_args
    print(f"JIT im Build:   {'✅ Ja' if has_jit_build else '❌ Nein'}")
    print("(Für echten JIT-Status siehe deep_jit_test() unten)")

    # # Indirekter JIT Check (Compiler Flags)
    # import _opcode
    # for x in dir(_opcode):
    #     print(x)
    #
    # has_jit = any(x for x in dir(_opcode) if "JIT" in x) # Grober Check
    # print(f"JIT Support:    {'Möglich' if has_jit else 'Unwahrscheinlich'}")


def deep_jit_test() -> None:

    print(f"--- Python JIT Diagnose für {sys.version.split()[0]} ---")

    # ---------------------------------------------------------
    # SCHRITT 1: Build-Konfiguration prüfen
    # ---------------------------------------------------------
    config_args = sysconfig.get_config_vars().get("CONFIG_ARGS") or ""
    print(f"{config_args=}")
    jit_compiled = "enable-experimental-jit" in config_args

    print(
        f"1. Build Config:      {'✅ Mit JIT Support kompiliert' if jit_compiled else '❌ Kein JIT im Build gefunden'}"
    )

    if not jit_compiled:
        print("   -> Abbruch: Dieser Python-Build kann keinen JIT nutzen.")
        return

    # ---------------------------------------------------------
    # SCHRITT 2: Umgebungsvariablen prüfen
    # ---------------------------------------------------------
    env_jit = os.environ.get("PYTHON_JIT")

    if env_jit == "0":
        print("2. Runtime Setting:   ❌ Deaktiviert via PYTHON_JIT=0")
        return
    elif env_jit == "1":
        print("2. Runtime Setting:   ✅ Aktiviert via PYTHON_JIT=1")
    else:
        print("2. Runtime Setting:   Default (vermutlich an, da im Build aktiv)")

    # ---------------------------------------------------------
    # SCHRITT 3: Der "Deep Check" (Introspektion)
    # ---------------------------------------------------------
    print("3. Deep Inspection:   Lasse Code heiß laufen...")

    try:
        import _opcode
    except ImportError:
        print("   -> Fehler: Modul '_opcode' fehlt (ungewöhnlich für 3.13+ dev builds).")
        return

    # Eine Funktion, die wir oft aufrufen, um den JIT zu triggern
    def hot_function(n: int) -> int:
        res = 0
        for i in range(n):
            res += i
        return res

    # Wir müssen die Funktion oft genug aufrufen
    for _ in range(5000):
        hot_function(10)

    # Jetzt prüfen wir das Code-Objekt der Funktion
    code_obj = hot_function.__code__

    # Versuche verschiedene Ansätze für die JIT-Erkennung
    try:
        # Ansatz 1: Prüfe alle gültigen Bytecode-Offsets
        executor_found = False

        # Iteriere durch alle möglichen Offsets im Bytecode
        for offset in range(0, len(code_obj.co_code), 2):  # Bytecode ist in 2-Byte-Einheiten
            try:
                executor = _opcode.get_executor(code_obj, offset)
                if executor:
                    print(f"   -> STATUS: ✅ JIT IST AKTIV!")
                    print(f"   -> Beweis: Executor Objekt bei Offset {offset}: {executor}")
                    executor_found = True
                    break
            except ValueError:
                # Dieser Offset hat keinen Executor, weiter zum nächsten
                continue

        if not executor_found:
            print(f"   -> STATUS: ⚠️ JIT scheint inaktiv (kein Executor an keinem Offset gefunden).")
            print("      (Mögliche Gründe: Code nicht heiß genug, JIT deaktiviert, oder API geändert)")

    except AttributeError:
        # Fallback falls sich die API geändert hat
        print("   -> Warnung: _opcode.get_executor existiert nicht.")
        print("      Das deutet oft darauf hin, dass der JIT im Build fehlt.")
    except Exception as e:
        print(f"   -> Unerwarteter Fehler bei der JIT-Prüfung: {e}")


if __name__ == "__main__":
    info_test()
    deep_jit_test()

# docker run -i --rm --name python314ephemeral --env PYTHON_JIT=1 xomoxcc/python314-jit:trixie < checkpython.py
