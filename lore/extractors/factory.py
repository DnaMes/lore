from typing import List

from .aider import AiderExtractor
from .antigravity import AntigravityExtractor
from .base import BaseExtractor
from .claude import ClaudeCodeExtractor
from .cline import ClineExtractor, KiloCodeExtractor, RooCodeExtractor
from .codex import CodexExtractor
from .commandcode import CommandCodeExtractor
from .copilot import CopilotCLIExtractor
from .crush import CrushExtractor
from .cursor import CursorExtractor
from .factory_droid import FactoryDroidExtractor
from .gemini import GeminiCLIExtractor
from .goose import GooseExtractor
from .hermes import HermesExtractor
from .kimi import KimiExtractor
from .opencode import OpenCodeExtractor
from .pi import PiExtractor
from .qwen import QwenExtractor
from .vscode import VSCodeCopilotExtractor
from .warp import WarpExtractor
from .zed import ZedExtractor


def get_all_extractors() -> List[BaseExtractor]:
    return [
        ClaudeCodeExtractor(),
        GeminiCLIExtractor(),
        CodexExtractor(),
        WarpExtractor(),
        CursorExtractor(),
        VSCodeCopilotExtractor(),
        CopilotCLIExtractor(),
        OpenCodeExtractor(),
        AntigravityExtractor(),
        AiderExtractor(),
        PiExtractor(),
        CommandCodeExtractor(),
        HermesExtractor(),
        ClineExtractor(),
        RooCodeExtractor(),
        KiloCodeExtractor(),
        GooseExtractor(),
        CrushExtractor(),
        KimiExtractor(),
        QwenExtractor(),
        ZedExtractor(),
        FactoryDroidExtractor(),
    ]
