"""
CLI entry point: generate narration, record, and mux a beat-based tutorial
video for one feature module under tutorial_studio/features/.

Usage: python3 -m tutorial_studio.build <feature>
Example: python3 -m tutorial_studio.build campaigns
"""
from tutorial_studio.lib import main

if __name__ == "__main__":
    main()
