import logging
import os
import re
from tempfile import mkdtemp

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command"""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! I'm a YouTube Downloader Bot.\n\n"
        "Simply send me a YouTube link, and I'll fetch it for you in your preferred quality.\n\n"
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
        "1. Send a YouTube link\n"
        "2. Select your preferred video quality\n"
        "3. Wait for the download to complete\n\n"
        "*Note:* This bot can download videos up to 2GB in size."
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
            r'^((?:https?:)?//)?((?:www|m)\.)?(youtube(-nocookie)?\.com|youtu.be)(/(?:[\w\-]+\?v=|embed/|live/|v/)?)([\w\-]+)(\S+)?$',
            url):
        await update.message.reply_text("Please send a valid YouTube URL.")
        return

    # Show processing message
    status_message = await update.message.reply_text("⏳ Fetching video information...")

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
                            InlineKeyboardButton(label, callback_data=f"{height}|{url}")
                        )

            # If no buttons were created (no proper video formats found), offer common resolutions
            if not buttons:
                logger.warning("No video formats detected from the API, using default resolutions")
                for res in [1080, 720, 480]:
                    buttons.append(
                        InlineKeyboardButton(f"{res}p", callback_data=f"{res}|{url}")
                    )

            # Add a button for audio only
            buttons.append(
                InlineKeyboardButton("Audio Only", callback_data=f"audio|{url}")
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
                await status_message.edit_text(
                    f"*{video_title}*\n\n"
                    f"Duration: {format_duration(duration)}\n"
                    f"Channel: {uploader}\n\n"
                    "Select video quality:",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )

    except DownloadError as e:
        logger.error(f"YouTube download error: {e}")
        await status_message.edit_text(f"❌ YouTube error: {str(e)}")
    except Exception as e:
        logger.error(f"Error processing URL: {e}")
        await status_message.edit_text(f"❌ Error processing video. Please try again. Error: {str(e)[:100]}")


async def handle_quality_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for quality selection callback"""
    query = update.callback_query
    await query.answer()

    # Get the message from the callback query and fallback to effective chat if needed
    message = query.message
    chat_id = update.effective_chat.id

    # Store original message for later editing if possible
    original_message = None
    if isinstance(message, Message):
        original_message = message
        status_message = message
    else:
        # Create a new status message if we can't edit the original
        status_message = await context.bot.send_message(
            chat_id=chat_id,
            text="Processing your request..."
        )

    # Parse callback data
    quality, url = query.data.split("|", 1)

    # Update status message
    status_text = "⏳ Downloading audio..." if quality == "audio" else f"⏳ Downloading {quality}p video..."
    if original_message:
        try:
            # For messages with photos, we need to edit caption instead
            if hasattr(original_message, 'photo') and original_message.photo:
                await original_message.edit_caption(
                    caption=status_text
                )
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
        await status_message.edit_text(status_text)

    # Create a temporary directory for downloads
    temp_dir = mkdtemp()
    download_success = False

    try:
        # Configure download options
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
                "socket_timeout": 30,  # Faster timeout for connections
                "retries": 3,  # Fewer retries for quicker failure
                "fragment_retries": 3,  # Fewer fragment retries
                "concurrent_fragment_downloads": 10,  # More concurrent downloads
                "throttledratelimit": 100000,
            }
        else:
            # Use a more reliable format selector string
            ydl_opts = {
                "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best",
                "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "geo_bypass": True,
                "socket_timeout": 30,  # Faster timeout for connections
                "retries": 3,  # Fewer retries for quicker failure
                "fragment_retries": 3,  # Fewer fragment retries
                "concurrent_fragment_downloads": 10,  # More concurrent downloads
                "throttledratelimit": 100000,

            }

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
            error_text = f"❌ File size ({file_size / (1024 * 1024):.1f}MB) exceeds the 2GB limit."
            if hasattr(original_message, 'photo') and original_message.photo:
                await original_message.edit_caption(caption=error_text)
            else:
                await status_message.edit_text(error_text)
            return

        # Send progress message
        progress_text = f"⏳ Uploading ({file_size / (1024 * 1024):.1f}MB)..."
        if original_message:
            if hasattr(original_message, 'photo') and original_message.photo:
                await original_message.edit_caption(caption=progress_text)
            else:
                await original_message.edit_text(progress_text)
        else:
            await status_message.edit_text(progress_text)

        # Send the file
        if quality == "audio":
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=open(output_path, "rb"),
                caption=f"{video_title} (Audio)",
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
                caption=f"{video_title} ({quality}p)",
                supports_streaming=True,
                duration=info.get("duration", None),
                width=resolution,
                height=resolution,
            )

        # Update completion message
        completion_text = "✅ Download complete!"
        if original_message:
            if hasattr(original_message, 'photo') and original_message.photo:
                await original_message.edit_caption(caption=completion_text)
            else:
                await original_message.edit_text(completion_text)
        else:
            await status_message.edit_text(completion_text)

    except Exception as e:
        logger.error(f"Download error: {e}")
        error_text = f"❌ Download failed: {str(e)[:100]}"
        if not download_success:
            if original_message:
                if hasattr(original_message, 'photo') and original_message.photo:
                    await original_message.edit_caption(caption=error_text)
                else:
                    await original_message.edit_text(error_text)
            else:
                await status_message.edit_text(error_text)
    finally:
        # Clean up temporary files
        cleanup_temp_files(temp_dir)


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
    application.add_handler(CallbackQueryHandler(handle_quality_selection))

    # Start the Bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()