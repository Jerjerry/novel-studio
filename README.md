# Universal Novel Studio Pro

AI-powered novel generation tool with a PyQt6 GUI. Write entire novels chapter-by-chapter using free LLM API keys from alistaitsacle/free-llm-api-keys.

## Features

✨ **Full Novel Generation**
- Auto-generate chapter titles
- Sequential or parallel chapter generation
- Memory chaining (chapters remember previous ones)
- Multiple quality presets (Short Story → Epic Novel)
- Multi-iteration refinement with optional "deepening"

✏️ **Chapter Rewriting**
- Rewrite individual chapters with custom instructions
- Target word count control

🔑 **Key Management**
- Auto-fetch fresh keys from verified sources
- Manual key addition
- Key rotation on 401/403/429 errors
- Rate limit handling

📊 **UI Features**
- Dark theme (Catppuccin)
- Real-time generation logs
- Live text streaming (optional)
- Progress tracking
- Settings persistence

## Installation

```bash
pip install PyQt6 openai requests
```

## Usage

```bash
python tryupgrade.py
```

### First Time Setup

1. **Create a blueprint**: Write a text file describing your novel's plot/outline
2. **Select output folder**: Where chapters will be saved
3. **Configure settings**:
   - Book title (optional - auto-generates)
   - Quality preset (chapters × words per chapter)
   - Genre and book type
4. **Click "Start Generation"**

### API Keys

Keys are auto-fetched from the alistaitsacle/free-llm-api-keys repository:
- Updated 3-5x daily
- $20-$100 budget per key
- Expire in 24-48 hours
- Shared publicly (others may consume budget)

If keys run out:
- Click "🔄 Refresh Keys" button
- Or add manual keys via "➕ Add Manual Key"

## Configuration

### Advanced Settings

- **Iterations**: Generate multiple versions
- **Deepen Story**: Use previous version as blueprint for next iteration
- **Parallel Mode**: Generate chapters simultaneously (faster)
- **Live Typing**: Stream text as it's generated (incompatible with parallel mode)
- **Custom System Prompt**: Modify author voice/style

## Output

Generated content is saved to:
```
output_folder/
├── Novel_Title_iter1/
│   ├── chapter_01_Title.txt
│   ├── chapter_02_Title.txt
│   ├── full_novel.txt
│   └── full_novel_Formatted.md
└── Novel_Title_iter2/
    └── ...
```

## Keyboard Shortcuts

- `Ctrl+G` - Start generation
- `Ctrl+Shift+S` - Stop generation
- `Ctrl+S` - Save settings

## Proxy & Model

- **Proxy**: https://aiapiv2.pekpik.com/v1
- **Model**: gpt-5.5
- **Max Tokens**: 4000
- **Rate Limit**: 20 RPM (4 second delay between requests)

## Database

Data is stored in `~/.novel_studio/`:
- `novel_studio.db` - SQLite database with keys and chapter tasks
- `config.json` - Application settings

## Troubleshooting

### All keys marked as dead

This means the keys from the repo are already used up by others. Solutions:
1. Wait 1-2 hours for fresh keys to be published
2. Click "🔄 Refresh Keys" to try again
3. Add your own key manually

### Rate limit errors

The app automatically waits 15s when hitting 429 errors. This is normal with shared free keys.

### Generation stops unexpectedly

Check the log for:
- API key errors (mark as dead, rotate to next)
- Network errors (retries automatically)
- Prompt syntax issues

## License

MIT

## Credits

API keys provided by [alistaitsacle/free-llm-api-keys](https://github.com/alistaitsacle/free-llm-api-keys)
