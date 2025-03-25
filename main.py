import logging
import os
import re
from tempfile import mkdtemp
import asyncio

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from typing import cast

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize bot with token from BotFather
TOKEN = os.getenv("BOT_TOKEN")

# Local Bot API Server URL (Important!)
LOCAL_API_SERVER = "http://localhost:8081/bot"  # Change to your local API server address

# Constants
MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2000MB with local API server
DOWNLOAD_BATCH_SIZE = 5  # Download and send videos in batches of 5


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command"""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! I'm a YouTube Downloader Bot.\n\n"
        "Simply send me a YouTube link or playlist, and I'll fetch it for you in your preferred quality.\n\n"
        "Use /help to see all available commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /help command"""
    help_text = (
        "📹 *YouTube Downloader Bot Help* 📹\n\n"
        "*Commands:*\n"
        "/start - Start the bot\n"
        "/help - Show this help message\n"
        "/cancel - Cancel the current download\n\n"
        "*How to use:*\n"
        "1. Send a YouTube video link or playlist link\n"
        "2. Select your preferred video quality or playlist download options\n"
        "3. Wait for the download to complete\n\n"
        "*Features:*\n"
        "• Download individual videos\n"
        "• Download entire playlists\n"
        "• Select specific videos from playlists\n"
        "• Extract audio from videos\n\n"
        "*Note:* Videos can be up to 2GB in size."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler to cancel ongoing downloads"""
    user_id = update.effective_user.id
    if 'download_task' in context.chat_data:
        context.chat_data['download_task'].cancel()
        context.chat_data['download_task'] = None
        await update.message.reply_text("Download canceled.")
    else:
        await update.message.reply_text("No active download to cancel.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for YouTube URLs"""
    url = update.message.text

    # YouTube URL validation with comprehensive regex
    if not re.match(
            r'^((?:https?:)?//)?((?:www|m)\.)?(youtube(-nocookie)?\.com|youtu.be)(/(?:[\w\-]+\?v=|embed/|live/|v/|playlist\?list=)?)([\w\-]+)(\S+)?$',
            url):
        await update.message.reply_text("Please send a valid YouTube URL.")
        return

    # Show processing message
    status_message = cast(Message, await update.message.reply_text("⏳ Fetching information..."))

    try:
        # Check if this is a playlist URL
        if "playlist" in url or "list=" in url:
            await handle_playlist(update, context, url, status_message)
            return

        # Process as a single video
        await process_single_video(update, context, url, status_message)

    except DownloadError as e:
        logger.error(f"YouTube download error: {e}")
        await status_message.edit_text(f"❌ YouTube error: {str(e)}")
    except Exception as e:
        logger.error(f"Error processing URL: {e}")
        await status_message.edit_text(f"❌ Error processing video. Please try again. Error: {str(e)[:100]}")


async def handle_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE, url, status_message):
    """Handler for YouTube playlist URLs"""
    msg = cast(Message, status_message)
    await msg.edit_text("⏳ Fetching playlist information...")

    # Store the URL in context.chat_data with a unique ID
    url_id = str(hash(url) % 10000)  # Create a short unique ID
    if 'urls' not in context.chat_data:
        context.chat_data['urls'] = {}
    context.chat_data['urls'][url_id] = url

    try:
        # Configure YoutubeDL to extract playlist information
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "extract_flat": True,  # Don't download, just get info
            "skip_download": True,
            "noplaylist": False,  # Process as playlist
        }

        # Get playlist info
        with YoutubeDL(ydl_opts) as ydl:
            playlist_info = ydl.extract_info(url, download=False)

            if not playlist_info:
                await status_message.edit_text("❌ Could not fetch playlist information.")
                return

            # Extract playlist details
            playlist_title = playlist_info.get('title', 'Unknown Playlist')
            entries = playlist_info.get('entries', [])

            if not entries:
                await status_message.edit_text("❌ This playlist is empty or private.")
                return

            # Check the number of videos in the playlist
            video_count = len(entries)

            # Create buttons for playlist options
            buttons = [
                [InlineKeyboardButton(f"Download All ({video_count} videos)",
                                      callback_data=f"playlist_all_{url_id}")],
                [InlineKeyboardButton("Select Individual Videos",
                                      callback_data=f"playlist_select_{url_id}")],
                [InlineKeyboardButton("First 5 Videos Only",
                                      callback_data=f"playlist_first5_{url_id}")],
                [InlineKeyboardButton("Audio Only (All Videos)",
                                      callback_data=f"playlist_audio_{url_id}")]
            ]

            # Create keyboard for playlist options
            keyboard = InlineKeyboardMarkup(buttons)

            # Send playlist information
            await status_message.edit_text(
                f"*Playlist: {playlist_title}*\n\n"
                f"Videos: {video_count}\n"
                f"Choose download option:",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

    except Exception as e:
        logger.error(f"Error processing playlist: {e}")
        await status_message.edit_text(f"❌ Error processing playlist. Please try again. Error: {str(e)[:100]}")


async def process_single_video(update: Update, context: ContextTypes.DEFAULT_TYPE, url, status_message):
    """Process a single video URL"""
    try:
        # Configure YoutubeDL to extract video information
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "extract_flat": False,
            "skip_download": True,
            "format": "best",
            "writeinfojson": False,
            "noplaylist": True,
        }

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if not info:
                await status_message.edit_text("❌ Could not fetch video information.")
                return

            # Check if the video is a livestream
            if info.get("is_live", False):
                await status_message.edit_text("❌ Cannot download live streams.")
                return

            # Get available formats with video and audio
            formats = [
                f for f in info.get("formats", [])
                if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("height")
            ]

            # If no combined formats, look for separate video and audio formats
            if not formats:
                video_formats = [
                    f for f in info.get("formats", [])
                    if f.get("vcodec") != "none" and f.get("height") and f.get("height") >= 480
                ]

                if video_formats:
                    formats = video_formats  # We'll use these and let yt-dlp handle merging

            # Debug output for format inspection
            logger.info(f"Available formats: {len(formats)}")
            for f in formats[:5]:  # Log first 5 formats for debugging
                logger.info(f"Format: {f.get('format_id')} - {f.get('height')}p - {f.get('ext')}")

            # Create quality selection buttons
            buttons = []
            unique_resolutions = set()

            # Sort formats by resolution (highest first)
            for f in sorted(formats, key=lambda x: (x.get("height", 0) or 0), reverse=True):
                height = f.get("height", 0)
                if 480 <= height <= 2160:  # Filter to reasonable resolutions
                    res = f"{height}p"
                    if res not in unique_resolutions:
                        unique_resolutions.add(res)
                        file_size_mb = f.get("filesize", 0) / (1024 * 1024)
                        if file_size_mb > 0:
                            label = f"{res} (~{file_size_mb:.1f}MB)"
                        else:
                            label = res
                        buttons.append(
                            InlineKeyboardButton(label, callback_data=f"quality|{height}|{url}")
                        )

            # If no buttons were created (no proper video formats found), offer common resolutions
            if not buttons:
                logger.warning("No video formats detected from the API, using default resolutions")
                for res in [1080, 720, 480]:
                    buttons.append(
                        InlineKeyboardButton(f"{res}p", callback_data=f"quality|{res}|{url}")
                    )

            # Add a button for audio only
            buttons.append(
                InlineKeyboardButton("Audio Only", callback_data=f"quality|audio|{url}")
            )

            # Arrange buttons in rows of 2
            keyboard = InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])

            # Get thumbnail URL
            thumbnail_url = info.get("thumbnail", "")
            video_title = info.get("title", "Unknown Title")
            uploader = info.get("uploader", "Unknown")
            duration = info.get("duration", 0)

            # First try to send with thumbnail
            try:
                if thumbnail_url:
                    # Send the thumbnail with video information
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=thumbnail_url,
                        caption=f"*{video_title}*\n\n"
                                f"Duration: {format_duration(duration)}\n"
                                f"Channel: {uploader}\n\n"
                                "Select video quality:",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    await status_message.delete()
                else:
                    # Fallback to text only if no thumbnail
                    await status_message.edit_text(
                        f"*{video_title}*\n\n"
                        f"Duration: {format_duration(duration)}\n"
                        f"Channel: {uploader}\n\n"
                        "Select video quality:",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
            except Exception as e:
                logger.error(f"Error sending thumbnail: {e}")
                # Fallback to text only
                msg = cast(Message, status_message)
                await msg.edit_text(
                    f"*{video_title}*\n\n"
                    f"Duration: {format_duration(duration)}\n"
                    f"Channel: {uploader}\n\n"
                    "Select video quality:",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )

    except Exception as e:
        logger.error(f"Error processing video: {e}")
        await status_message.edit_text(f"❌ Error processing video. Please try again. Error: {str(e)[:100]}")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrapper for handling callback queries and routing to appropriate handlers"""
    query = update.callback_query
    callback_data = query.data

    # Handle different callback formats
    if callback_data.startswith("quality|"):
        # Single video quality selection
        parts = callback_data.split('|')
        quality = parts[1]
        url = parts[2]
        await handle_quality_selection(update, context, quality, url)
    elif callback_data.startswith("playlist_"):
        # Playlist actions - handle with underscores instead of pipes
        parts = callback_data.split('_')
        if len(parts) >= 3:
            playlist_action = parts[1]
            url_id = parts[2]
            # Retrieve the actual URL from context
            if 'urls' in context.chat_data and url_id in context.chat_data['urls']:
                url = context.chat_data['urls'][url_id]
                await handle_playlist_action(update, context, playlist_action, url)
            else:
                await query.answer("URL not found. Please try again.")
        else:
            await query.answer("Invalid callback data")
    elif callback_data.startswith("playlist_video|"):
        # Individual playlist video selection
        parts = callback_data.split('|')
        video_id = parts[1]
        quality = parts[2]
        await handle_quality_selection(update, context, quality, f"https://www.youtube.com/watch?v={video_id}")
    elif callback_data.startswith("video_info|"):
        # Video info button was clicked
        parts = callback_data.split('|')
        video_id = parts[1]
        # Handle video info display if needed
        await query.answer(f"Video ID: {video_id}")
    else:
        await query.answer("Unknown action")

async def handle_playlist_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action, url):
    """Handle playlist download actions"""
    query = update.callback_query
    message = cast(Message, query.message)
    chat_id = update.effective_chat.id

    # Update message to show processing
    await message.edit_text(f"⏳ Processing playlist action: {action}...")

    try:
        # Configure YoutubeDL to extract playlist information
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "extract_flat": True,  # Don't download, just get info
            "skip_download": True,
            "noplaylist": False,  # Process as playlist
        }

        # Get playlist info
        with YoutubeDL(ydl_opts) as ydl:
            playlist_info = ydl.extract_info(url, download=False)

            if not playlist_info:
                await message.edit_text("❌ Could not fetch playlist information.")
                return

            # Extract playlist details
            playlist_title = playlist_info.get('title', 'Unknown Playlist')
            entries = playlist_info.get('entries', [])

            if not entries:
                await message.edit_text("❌ This playlist is empty or private.")
                return

            # Process based on action
            if action == "all":
                # Download all videos in playlist
                video_count = len(entries)
                info_message = await message.edit_text(
                    f"⏳ Starting download of {video_count} videos from playlist: {playlist_title}\n\n"
                    "Videos will be sent as they are downloaded."
                )

                # Process videos in batches to prevent memory issues
                for batch_start in range(0, video_count, DOWNLOAD_BATCH_SIZE):
                    batch_end = min(batch_start + DOWNLOAD_BATCH_SIZE, video_count)

                    # Create tasks for concurrent downloads
                    download_tasks = []
                    for idx in range(batch_start, batch_end):
                        entry = entries[idx]
                        if not entry:
                            continue

                        video_id = entry.get('id')
                        video_url = f"https://www.youtube.com/watch?v={video_id}"

                        # Submit download task
                        task = asyncio.create_task(
                            download_and_send_video(
                                context,
                                chat_id,
                                "720",  # Default to 720p
                                video_url,
                                f"[{idx + 1}/{video_count}]",
                                info_message
                            )
                        )
                        download_tasks.append(task)

                    # Wait for all tasks in batch to complete
                    if download_tasks:
                        await info_message.edit_text(
                            f"⏳ Downloading batch {batch_start // DOWNLOAD_BATCH_SIZE + 1} of "
                            f"{(video_count - 1) // DOWNLOAD_BATCH_SIZE + 1} from playlist: {playlist_title}\n\n"
                            f"Processing videos {batch_start + 1}-{batch_end} of {video_count}"
                        )
                        await asyncio.gather(*download_tasks)

                await info_message.edit_text(f"✅ Playlist download complete: {playlist_title}")

            elif action == "first5":
                # Download first 5 videos
                limit = min(5, len(entries))
                info_message = await message.edit_text(
                    f"⏳ Starting download of first {limit} videos from playlist: {playlist_title}\n\n"
                    "Videos will be sent as they are downloaded."
                )

                # Create tasks for concurrent downloads
                download_tasks = []
                for idx in range(limit):
                    entry = entries[idx]
                    if not entry:
                        continue

                    video_id = entry.get('id')
                    video_url = f"https://www.youtube.com/watch?v={video_id}"

                    # Submit download task
                    task = asyncio.create_task(
                        download_and_send_video(
                            context,
                            chat_id,
                            "720",  # Default to 720p
                            video_url,
                            f"[{idx + 1}/{limit}]",
                            info_message
                        )
                    )
                    download_tasks.append(task)

                # Wait for all tasks to complete
                if download_tasks:
                    await asyncio.gather(*download_tasks)

                await info_message.edit_text(f"✅ First {limit} videos downloaded from playlist: {playlist_title}")

            elif action == "audio":
                # Download audio for all videos
                video_count = len(entries)
                info_message = await message.edit_text(
                    f"⏳ Starting audio download for {video_count} videos from playlist: {playlist_title}\n\n"
                    "Audio files will be sent as they are downloaded."
                )

                # Process in batches to prevent memory issues
                for batch_start in range(0, video_count, DOWNLOAD_BATCH_SIZE):
                    batch_end = min(batch_start + DOWNLOAD_BATCH_SIZE, video_count)

                    # Create tasks for concurrent downloads
                    download_tasks = []
                    for idx in range(batch_start, batch_end):
                        entry = entries[idx]
                        if not entry:
                            continue

                        video_id = entry.get('id')
                        video_url = f"https://www.youtube.com/watch?v={video_id}"

                        # Submit download task
                        task = asyncio.create_task(
                            download_and_send_video(
                                context,
                                chat_id,
                                "audio",  # Audio only
                                video_url,
                                f"[{idx + 1}/{video_count}]",
                                info_message
                            )
                        )
                        download_tasks.append(task)

                    # Wait for all tasks in batch to complete
                    if download_tasks:
                        await info_message.edit_text(
                            f"⏳ Downloading audio batch {batch_start // DOWNLOAD_BATCH_SIZE + 1} of "
                            f"{(video_count - 1) // DOWNLOAD_BATCH_SIZE + 1} from playlist: {playlist_title}\n\n"
                            f"Processing audio {batch_start + 1}-{batch_end} of {video_count}"
                        )
                        await asyncio.gather(*download_tasks)

                await info_message.edit_text(f"✅ Playlist audio download complete: {playlist_title}")

            elif action == "select":
                # Create a list of videos to select from
                limit = min(30, len(entries))  # Limit selections to 30 for UI reasons
                buttons = []

                # Create buttons for each video
                for idx, entry in enumerate(entries[:limit]):
                    if not entry:
                        continue

                    video_id = entry.get('id')
                    video_title = entry.get('title', f'Video {idx + 1}')

                    # Create quality options for each video
                    video_buttons = [
                        [
                            InlineKeyboardButton(f"{idx + 1}. {video_title[:20]}...",
                                                 callback_data=f"video_info|{video_id}")
                        ],
                        [
                            InlineKeyboardButton("720p", callback_data=f"playlist_video|{video_id}|720"),
                            InlineKeyboardButton("480p", callback_data=f"playlist_video|{video_id}|480"),
                            InlineKeyboardButton("Audio", callback_data=f"playlist_video|{video_id}|audio")
                        ]
                    ]

                    buttons.extend(video_buttons)

                # Create keyboard with videos
                keyboard = InlineKeyboardMarkup(buttons)

                await message.edit_text(
                    f"*Select videos from playlist:* {playlist_title}\n\n"
                    "Choose quality option for each video you want to download:",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )

    except Exception as e:
        logger.error(f"Error processing playlist action: {e}")
        await message.edit_text(f"❌ Error processing playlist. Please try again. Error: {str(e)[:100]}")


async def download_and_send_video(context, chat_id, quality, url, progress_label, info_message=None):
    """Download and send a single video or audio file"""
    temp_dir = mkdtemp()
    download_success = False
    output_path = None
    info = None

    try:
        # Configure download options for improved speed
        if quality == "audio":
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "quiet": True,
                "no_warnings": True,
                "geo_bypass": True,
                "socket_timeout": 30,
                "retries": 3,
                "fragment_retries": 3,
                "concurrent_fragment_downloads": 10,
                "throttledratelimit": 100000,
            }
        else:
            ydl_opts = {
                "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best",
                "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "geo_bypass": True,
                "socket_timeout": 30,
                "retries": 3,
                "fragment_retries": 3,
                "concurrent_fragment_downloads": 10,
                "throttledratelimit": 100000,
            }

        # Update info message if provided
        if info_message:
            current_text = await get_message_text(info_message)
            if not current_text.endswith(f"\nDownloading {progress_label}..."):
                await update_info_message(info_message, f"{current_text}\nDownloading {progress_label}...")

        # Download the video
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_title = info.get("title", "video")
            download_success = True

        # Find the downloaded file
        downloaded_files = os.listdir(temp_dir)
        if not downloaded_files:
            raise Exception("No files were downloaded")

        output_path = os.path.join(temp_dir, downloaded_files[0])
        file_size = os.path.getsize(output_path)

        # Check if the file exceeds the local API server limit
        if file_size > MAX_FILE_SIZE:
            raise Exception(f"File size ({file_size / (1024 * 1024):.1f}MB) exceeds the 2GB limit.")

        # Update info message if provided
        if info_message:
            current_text = await get_message_text(info_message)
            if "Uploading" not in current_text:
                await update_info_message(info_message,
                                          f"{current_text}\nUploading {progress_label} ({file_size / (1024 * 1024):.1f}MB)...")

        # Send the file
        if quality == "audio":
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=open(output_path, "rb"),
                caption=f"{video_title} {progress_label} (Audio)",
                title=video_title,
                performer=info.get("uploader", "Unknown"),
                duration=info.get("duration", None),
            )
        else:
            # Convert quality to integer for width/height parameters
            resolution = int(quality) if quality != "audio" else None

            await context.bot.send_video(
                chat_id=chat_id,
                video=open(output_path, "rb"),
                caption=f"{video_title} {progress_label} ({quality}p)",
                supports_streaming=True,
                duration=info.get("duration", None),
                width=resolution,
                height=resolution,
            )

        # Update info message after successful send
        if info_message:
            current_text = await get_message_text(info_message)
            updated_text = current_text.replace(
                f"Uploading {progress_label}",
                f"✅ Completed {progress_label}"
            )
            await update_info_message(info_message, updated_text)

        return True

    except Exception as e:
        logger.error(f"Download error ({progress_label}): {e}")

        # Update info message with error
        if info_message:
            current_text = await get_message_text(info_message)
            updated_text = current_text.replace(
                f"Downloading {progress_label}...",
                f"❌ Failed {progress_label}: {str(e)[:50]}"
            ).replace(
                f"Uploading {progress_label}",
                f"❌ Failed {progress_label}: {str(e)[:50]}"
            )
            await update_info_message(info_message, updated_text)

        return False
    finally:
        # Clean up temporary files
        cleanup_temp_files(temp_dir)


async def handle_quality_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, quality, url,
                                   is_playlist=False, progress_message=None, playlist_progress=None):
    """Handler for quality selection"""
    # Get message and chat ID based on whether this is from a callback query or playlist process
    if is_playlist:
        message = cast(Message, progress_message)
        chat_id = message.chat_id
        original_message = message
        status_message = message
    else:
        query = update.callback_query
        await query.answer()
        message = cast(Message, query.message)
        chat_id = update.effective_chat.id
        original_message = message
        status_message = message

    # Update status message
    if playlist_progress:
        status_text = f"⏳ {playlist_progress}: " + (
            "Downloading audio..." if quality == "audio" else f"Downloading {quality}p video...")
    else:
        status_text = "⏳ Downloading audio..." if quality == "audio" else f"⏳ Downloading {quality}p video..."

    if not is_playlist:
        try:
            # For messages with photos, we need to edit caption instead
            if hasattr(original_message, 'photo') and original_message.photo:
                await original_message.edit_caption(caption=status_text)
            else:
                await original_message.edit_text(status_text)
        except Exception as e:
            logger.error(f"Error editing message: {e}")
            # If editing fails, send a new message
            status_message = await context.bot.send_message(
                chat_id=chat_id,
                text=status_text
            )
    else:
        try:
            await status_message.edit_text(status_text)
        except Exception as e:
            logger.error(f"Error updating playlist message: {e}")

    # Use the download_and_send_video function
    progress_label = playlist_progress if playlist_progress else ""
    success = await download_and_send_video(context, chat_id, quality, url, progress_label, status_message)

    # Update completion message for single video downloads
    if not is_playlist and success:
        completion_text = "✅ Download complete!"
        try:
            if hasattr(original_message, 'photo') and original_message.photo:
                await original_message.edit_caption(caption=completion_text)
            else:
                await status_message.edit_text(completion_text)
        except Exception as e:
            logger.error(f"Error updating completion message: {e}")


async def get_message_text(message):
    """Get message text safely"""
    try:
        if isinstance(message, Message):
            return message.text or ""
        return ""
    except Exception:
        return ""


async def update_info_message(message, new_text):
    """Update info message safely"""
    try:
        if isinstance(message, Message):
            await message.edit_text(new_text)
        else:
            logger.warning(f"Expected Message object, got {type(message)}")
    except Exception as e:
        logger.error(f"Error updating info message: {e}")


def cleanup_temp_files(directory):
    """Clean up temporary files and directories"""
    try:
        if os.path.exists(directory):
            for filename in os.listdir(directory):
                file_path = os.path.join(directory, filename)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                except Exception as e:
                    logger.error(f"Error deleting {file_path}: {e}")
            os.rmdir(directory)
    except Exception as e:
        logger.error(f"Error cleaning up directory {directory}: {e}")

def format_duration(seconds):
    """Format seconds into hours:minutes:seconds"""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{int(hours)}:{int(minutes):02d}:{int(seconds):02d}"
    else:
        return f"{int(minutes):02d}:{int(seconds):02d}"


def main():
    """Start the bot"""
    # Create the Application with local API server
    application = Application.builder().token(TOKEN).base_url(LOCAL_API_SERVER).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    # Start the Bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()