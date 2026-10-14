from typing import List

from .aider import AiderExtractor
from .antigravity import AntigravityExtractor
from .base import BaseExtractor
from .claude import ClaudeCodeExtractor
from .codex import CodexExtractor
from .copilot import CopilotCLIExtractor
from .cursor import CursorExtractor
from .gemini import GeminiCLIExtractor
from .opencode import OpenCodeExtractor
from .pi import PiExtractor
from .vscode import VSCodeCopilotExtractor
from .warp import WarpExtractor


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
    ]

