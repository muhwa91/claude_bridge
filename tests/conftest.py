"""tests/ 에서 프로젝트 루트의 bridge.py 를 임포트할 수 있게 sys.path 에 루트를 추가."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
