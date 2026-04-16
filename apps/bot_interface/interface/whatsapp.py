import json
import os
from typing import Dict, Any
from django.utils import timezone
import bot_interface.interface.generic
import bot_interface.models
import bot_interface.utils
import bot_interface.api
import bot_interface.auth
import requests

from bot_interface.data_classes import EventPacket
import time
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import io
import math
import random
from bot_interface.helper import (
    _extract_whatsapp_value,
    _load_user_session,
    _normalize_location_response,
    _extract_lat_lon_from_session,
    _prepare_and_send_list,
    _extract_community_id_from_session,
    _build_community_data,
    _get_user_session,
    _extract_location_data,
    _archive_user_session,
    _reset_user_session,
    _get_bot_instance,
    _get_bot_user,
    _get_smj,
    _extract_ids_from_session,
    _extract_media_data,
    _detect_flow_type,
    _build_misc_payload,
    _resolve_community_id,
)
# from geoadmin.models import State, District, Block
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

CE_API_URL = getattr(settings, "CE_API_URL", "http://localhost:8000/api/v1/")
CE_BUCKET_NAME = getattr(settings, "CE_BUCKET_NAME", "corestack-bucket")
CE_BUCKET_URL = getattr(settings, "CE_BUCKET_URL", "https://corestack-bucket.s3.amazonaws.com/")


class WhatsAppInterface(bot_interface.interface.generic.GenericInterface):
    """WhatsApp interface implementation for handling WhatsApp Business API interactions"""

    @staticmethod
    def create_event_packet(
        json_obj: Any, bot_id: int, event: str = "start"
    ) -> Dict[str, Any]:
        """
        Create an event packet from WhatsApp webhook data.
        """

        print("create_event_packet called with bot_id:", bot_id, type(bot_id))

        try:
            bot_interface.models.Bot.objects.get(id=bot_id)
        except bot_interface.models.Bot.DoesNotExist:
            raise ValueError(f"Bot with id {bot_id} not found")

        # Parse JSON if string
        if isinstance(json_obj, str):
            json_obj = json.loads(json_obj)

        # Handle WhatsApp webhook list format
        if isinstance(json_obj, list) and len(json_obj) > 0:
            if "changes" in json_obj[0]:
                json_obj = json_obj[0]["changes"][0]["value"]

        # Base packet
        event_packet = {
            "event": event,
            "bot_id": bot_id,
            "data": "",
            "timestamp": "",
            "message_id": "",
            "media_id": "",
            "wa_id": "",
            "misc": "",
            "type": "",
            "user_number": "",
            "smj_id": "",
            "state": "",
            "context_id": "",
        }

        # Process incoming message
        if "contacts" in json_obj:
            WhatsAppInterface._process_message_data(json_obj, event_packet, bot_id)

        # Preserve session context (smj_id, state, user_id)
        WhatsAppInterface._preserve_user_context(event_packet, bot_id)

        # 🔥 CRITICAL FIX: normalize interactive events
        WhatsAppInterface._normalize_interactive_event(event_packet, bot_id)

        return event_packet

    @staticmethod
    def _normalize_interactive_event(event_packet: Dict, bot_id: int) -> None:
        """
        Convert WhatsApp interactive/button replies into semantic SMJ events.
        """

        if event_packet.get("type") != "button":
            return

        user_number = event_packet.get("user_number")
        if not user_number:
            return

        try:
            bot = bot_interface.models.Bot.objects.get(id=bot_id)
            user_session = bot_interface.models.UserSessions.objects.get(
                user__phone=user_number, bot=bot
            )
        except Exception:
            return

        # 🔑 COMMUNITY SELECTION CONTRACT
        if user_session.expected_response_type == "community":
            event_packet["event"] = "success"
            # misc already has community_id
            return

        # Default fallback
        event_packet["event"] = "success"

    @staticmethod
    def _process_message_data(json_obj: Dict, event_packet: Dict, bot_id: int) -> None:
        """Process regular WhatsApp message data"""

        # Extract contact info
        contact = json_obj.get("contacts", [{}])[0]
        wa_id = contact.get("wa_id", "")
        event_packet["user_number"] = wa_id
        event_packet["wa_id"] = wa_id

        # No messages present
        messages = json_obj.get("messages")
        if not messages:
            logger.warning(f"No messages found in event packet")
            return

        message = messages[0]
        data_type = message.get("type", "")

        event_packet["timestamp"] = message.get("timestamp", "")
        event_packet["message_id"] = message.get("id", "")
        event_packet["type"] = data_type

        # Message type routing
        handlers = {
            "text": WhatsAppInterface._process_text_message,
            "interactive": WhatsAppInterface._process_interactive_response,
            "location": WhatsAppInterface._process_location_message,
            "image": lambda m, e: WhatsAppInterface._process_image_message(
                m, e, bot_id
            ),
            "audio": lambda m, e: WhatsAppInterface._process_audio_message(
                m, e, bot_id
            ),
            "voice": lambda m, e: WhatsAppInterface._process_audio_message(
                m, e, bot_id
            ),
        }

        handler = handlers.get(data_type)
        if handler:
            handler(message, event_packet)

    @staticmethod
    def _process_interactive_response(message: Dict, event_packet: Dict) -> None:
        event_packet["type"] = "button"
        interactive = message["interactive"]

        if interactive.get("list_reply"):
            title = interactive["list_reply"]["title"]
            reply_id = interactive["list_reply"]["id"]
        else:
            title = interactive["button_reply"]["title"]
            reply_id = interactive["button_reply"]["id"]

        # Always keep UI text separate
        event_packet["data"] = title

        # Always keep raw id available
        event_packet["misc"] = reply_id

        # 🔑 IMPORTANT: set semantic event
        event_packet["event"] = reply_id

        if message.get("context"):
            event_packet["context_id"] = message["context"]["id"]

    @staticmethod
    def _download_and_upload_media(
        bot_id: int, mime_type: str, media_id: str, media_type: str
    ) -> Dict[str, str]:
        """Download media from WhatsApp and upload to S3"""
        # You need to implement these functions
        if media_type == "image":
            filepath = WhatsAppInterface._download_image(bot_id, mime_type, media_id)
        else:
            filepath = WhatsAppInterface._download_audio(bot_id, mime_type, media_id)

        # Upload to S3
        file_extension = bot_interface.utils.get_filename_extension(filepath)[1]
        s3_folder = "docs/images/" if media_type == "image" else "docs/audios/"
        file_name = s3_folder + filepath.split("/")[-1]

        print(f"Uploading {filepath} to S3 bucket {CE_BUCKET_NAME}")
        status, url, error = bot_interface.utils.push_to_s3(
            filepath, CE_BUCKET_NAME, file_name, file_extension
        )
        if status:
            print(f"URL:  {url}")
            return {"s3_url": url, "local_path": filepath}
        else:
            return {"s3_url": "Failed", "local_path": filepath}

    @staticmethod
    def _process_text_message(message: Dict, event_packet: Dict) -> None:
        """Process text message"""
        event_packet["type"] = "text"
        event_packet["data"] = message["text"]["body"]

    @staticmethod
    def _process_location_message(message: Dict, event_packet: Dict) -> None:
        """Process location message"""

        event_packet["type"] = "location"
        location = message["location"]

        # Extract latitude and longitude
        latitude = location.get("latitude", "")
        longitude = location.get("longitude", "")

        # Store as formatted string or coordinate object
        event_packet["data"] = f"{latitude},{longitude}"
        event_packet["misc"] = {
            "latitude": latitude,
            "longitude": longitude,
            "name": location.get("name", ""),
            "address": location.get("address", ""),
        }

        logger.info(f"Processed Location Message: {latitude} long: {longitude}")

    @staticmethod
    def _preserve_user_context(event_packet: Dict, bot_id: int) -> None:
        """Preserve current user context for proper state transitions"""
        try:
            user_number = event_packet["user_number"]
            if not user_number:
                logger.warning("No user number found in event packet")
                return

            # Look up the UserSession directly by phone number stored in the model
            try:
                bot_instance = bot_interface.models.Bot.objects.get(id=bot_id)
                user_session = bot_interface.models.UserSessions.objects.get(
                    phone=user_number, bot=bot_instance
                )

                # Preserve current SMJ and state context
                if user_session.current_smj and user_session.current_state:
                    event_packet["smj_id"] = user_session.current_smj.id
                    event_packet["state"] = user_session.current_state
                    logger.info(
                        f"Preserved user context - SMJ: {user_session.current_smj.id}, State: {user_session.current_state}"
                    )
                else:
                    logger.info("User session found but no SMJ/state context yet (new user)")

            except bot_interface.models.Bot.DoesNotExist:
                logger.error(f"Bot with id {bot_id} not found in _preserve_user_context")
            except bot_interface.models.UserSessions.DoesNotExist:
                # User or session doesn't exist yet — will be created in StartUserSession task
                logger.info("No existing UserSession found for %s, will create new one", user_number)

        except Exception as e:
            logger.error(f"Error preserving user context: {e}")
            # Don't fail the whole process if context preservation fails

    @staticmethod
    def _process_media_message(
        message: Dict,
        event_packet: Dict,
        bot_id: int,
        media_type: str,
    ) -> None:
        """
        Process WhatsApp media messages (image, audio, voice).
        """

        event_packet["type"] = media_type

        # Extract media block safely
        media_block = message.get(media_type) or message.get("voice")
        if not media_block:
            return

        media_id = media_block.get("id", "")
        mime_type = media_block.get("mime_type", "")

        event_packet["media_id"] = media_id

        # Store metadata for background processing in Celery
        event_packet["media_metadata"] = {
            "media_id": media_id,
            "mime_type": mime_type,
            "media_type": media_type,
            "bot_id": bot_id,
        }
        event_packet["needs_processing"] = True
        event_packet["data"] = ""  # Will be populated in Celery task

    @staticmethod
    def _process_image_message(message: Dict, event_packet: Dict, bot_id: int) -> None:
        WhatsAppInterface._process_media_message(
            message, event_packet, bot_id, media_type="image"
        )

    @staticmethod
    def _process_audio_message(message: Dict, event_packet: Dict, bot_id: int) -> None:
        WhatsAppInterface._process_media_message(
            message, event_packet, bot_id, media_type="audio"
        )

    def store_selected_community_and_context(self, bot_instance_id, data_dict):
        """
        Store selected community from menu and context.
        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Dictionary containing user and session data.
        Returns:
            str: "community_selected" or "failure"
        """
        print(
            f"DEBUG: store_selected_community_and_context called with bot_instance_id={bot_instance_id}"
        )
        print(f"DEBUG: data_dict keys: {list(data_dict.keys())}")
        print(f"DEBUG: data_dict contents: {data_dict}")

        try:
            bot_instance = bot_interface.models.Bot.objects.get(id=bot_instance_id)
            user_id = data_dict.get("user_id")
            print(f"DEBUG: bot_instance={bot_instance}, user_id={user_id}")

            # Get user session
            user = bot_interface.models.UserSessions.objects.get(
                user=user_id, bot=bot_instance
            )
            print(f"DEBUG: Found user session: {user}")

            # Extract community ID from button data or event
            community_id = None
            event_data = data_dict.get("event_data", {})
            print(f"DEBUG: event_data: {event_data}")
            print(f"DEBUG: event_data type: {event_data.get('type')}")
            if event_data.get("type") == "button":
                button_value = event_data.get("misc") or event_data.get("data")
                print(f"DEBUG: Button event detected - button_value: {button_value}")

                # ✅ Always preserve semantic event
                event_to_process = event_data.get("event") or "success"

                # -------- Payload handling ONLY --------

                if button_value == "continue_last_accessed":
                    print("DEBUG: User chose to continue with last accessed community")

                    bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)
                    success, api_response = (
                        bot_interface.utils.check_user_community_status_http(user.phone)
                    )

                    if success and api_response.get("success"):
                        community_id = (
                            api_response.get("data", {})
                            .get("misc", {})
                            .get("last_accessed_community_id")
                        )
                        print(
                            f"DEBUG: Got last accessed community ID from API: {community_id}"
                        )
                    else:
                        community_id = bot_user.user_misc.get(
                            "community_membership", {}
                        ).get("last_accessed_community_id")
                        print(
                            f"DEBUG: Got last accessed community ID from stored data: {community_id}"
                        )

                elif button_value and str(button_value).startswith("community_"):
                    community_id = button_value.split("_", 1)[1]
                    print(f"DEBUG: Extracted community ID from button: {community_id}")

                elif button_value and button_value.isdigit():
                    # Your current case: misc = "8"
                    community_id = button_value
                    print(f"DEBUG: Numeric community ID detected: {community_id}")
            else:
                # For non-button events, extract from event field
                event = data_dict.get("event", "")
                print(f"DEBUG: Non-button event - processing event: {event}")

                if event == "continue_last_accessed":
                    # User wants to continue with last accessed community
                    print(
                        f"DEBUG: User chose to continue with last accessed community (event)"
                    )
                    bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)
                    success, api_response = (
                        bot_interface.utils.check_user_community_status_http(user.phone)
                    )
                    if success and api_response.get("success"):
                        community_data = api_response.get("data", {})
                        community_id = community_data.get("misc", {}).get(
                            "last_accessed_community_id"
                        )
                        print(
                            f"DEBUG: Got last accessed community ID from API: {community_id}"
                        )
                    else:
                        # Fallback to stored data
                        community_id = bot_user.user_misc.get(
                            "community_membership", {}
                        ).get("last_accessed_community_id")
                        print(
                            f"DEBUG: Got last accessed community ID from stored data: {community_id}"
                        )
                elif event.startswith("community_"):
                    community_id = event.split("_")[1]
                    print(f"DEBUG: Extracted community ID from event: {community_id}")

            print(f"DEBUG: Final community_id: {community_id}")

            if community_id:
                # Store in UserSessions.misc_data
                if not user.misc_data:
                    user.misc_data = {}

                user.misc_data["active_community_id"] = community_id
                user.misc_data["navigation_context"] = "community_selection"
                user.misc_data["last_service_event"] = "choose_other"
                user.save()

                print(
                    f"DEBUG: Stored selected community {community_id} with context community_selection"
                )
                print(f"DEBUG: Returning 'community_selected'")
                return "community_selected"
            else:
                print(
                    f"DEBUG: Could not extract community ID from event data, returning 'failure'"
                )
                return "failure"

        except Exception as e:
            print(f"DEBUG: Exception in store_selected_community_and_context: {e}")
            import traceback

            traceback.print_exc()
            return "failure"

    def _store_media_data(
        self, bot_instance_id, data_dict, media_type, flow_type="work_demand"
    ):
        """
        Generic handler for audio/photo storage.
        """
        try:
            smj = _get_smj(data_dict.get("smj_id"))
            flow_type = getattr(smj, "name", None)
            print(f"Flow Type: {flow_type}")

            print(f"data dict : {data_dict}")
            user = _get_user_session(bot_instance_id, data_dict.get("user_id"))
            print(f"user: {user}")
            if not user:
                return "failure"

            media_data = _extract_media_data(user, data_dict, media_type)
            if not media_data:
                print(f"No {media_type} data found")
                return "failure"

            user.misc_data = user.misc_data or {}
            user.misc_data.setdefault(flow_type, {})

            if media_type == "audio":
                user.misc_data[flow_type]["audio"] = media_data
            else:
                user.misc_data[flow_type].setdefault("photos", [])
                if isinstance(media_data, list):
                    user.misc_data[flow_type]["photos"].extend(media_data)
                else:
                    user.misc_data[flow_type]["photos"].append(media_data)

            user.save()
            print(f"Stored {media_type} data for {flow_type}: {media_data}")
            return "success"

        except Exception:
            logger.exception(f"_store_media_data failed for {media_type}")
            return "failure"

    @staticmethod
    def _download_media(
        bot_id: int,
        mime_type: str,
        media_id: str,
        media_type: str,
    ) -> str:
        """
        Download media (image, audio, voice, video) from WhatsApp API.
        """

        try:
            logger.info(
                "Downloading %s | bot_id=%s | mime_type=%s | media_id=%s",
                media_type,
                bot_id,
                mime_type,
                media_id,
            )

            # Select correct API method
            download_fn_map = {
                "image": bot_interface.api.download_image,
                "audio": bot_interface.api.download_audio,
                "voice": bot_interface.api.download_audio,
            }

            download_fn = download_fn_map.get(media_type)
            if not download_fn:
                raise ValueError(f"Unsupported media type: {media_type}")

            response, filepath = download_fn(bot_id, mime_type, media_id)

            if response and response.status_code == 200 and filepath:
                logger.info(
                    "%s downloaded successfully: %s",
                    media_type.capitalize(),
                    filepath,
                )
                return filepath

            raise RuntimeError(
                f"{media_type.capitalize()} download failed | "
                f"status={response.status_code if response else 'None'} | "
                f"filepath={filepath}"
            )

        except Exception as exc:
            logger.exception("Error downloading %s media", media_type)
            raise

    @staticmethod
    def _download_image(bot_id: int, mime_type: str, media_id: str) -> str:
        return WhatsAppInterface._download_media(
            bot_id, mime_type, media_id, media_type="image"
        )

    @staticmethod
    def _download_audio(bot_id: int, mime_type: str, media_id: str) -> str:
        return WhatsAppInterface._download_media(
            bot_id, mime_type, media_id, media_type="audio"
        )

    @staticmethod
    def _is_interactive_message(json_obj: Dict) -> bool:
        """Check if this is an interactive message"""
        return bool(json_obj.get("id") and json_obj.get("type") == "interactive")

    @staticmethod
    def _process_interactive_message(json_obj: Dict, event_packet: Dict) -> None:
        """Process interactive message"""
        event_packet["message_id"] = json_obj["id"]
        event_packet["message_to"] = json_obj.get("to", "")
        event_packet["type"] = json_obj["type"]

    def sendText(self, bot_id, data_dict):
        logger.info("data_dict in sendText: %s", data_dict)
        data = data_dict.get("text")
        user_id = data_dict.get("user_id")
        bot_instance, user_session = _load_user_session(bot_id=bot_id, user_id=user_id)
        # Support both formats: dict {'hi': '...', 'en': '...'} or list [{'hi': '...', 'en': '...'}]
        if isinstance(data, list):
            lang_dict = data[0] if data else {}
        elif isinstance(data, dict):
            lang_dict = data
        else:
            lang_dict = {}
        user_lang = user_session.user_config.get("language") or bot_instance.language or "hi"
        text = lang_dict.get(user_lang) or lang_dict.get("hi") or str(data)
        try:
            response = bot_interface.api.send_text(
                bot_instance_id=bot_id, contact_number=user_session.phone, text=text
            )
            user_session.expected_response_type = "text"
            user_session.current_state = data_dict.get("state")
            user_session.current_smj = bot_instance.smj
            user_session.save()
            if response and response.get("messages"):
                logger.info(
                    f"Whatsapp Text Message {text} send to {user_session.phone} under {user_session.id} session"
                )
                return "success"
            else:
                logger.info(
                    f"Whatsapp Text Message {text} send to {user_session.phone} under {user_session.id} session Failed {response}"
                )
                return "failure"

        except Exception as e:
            logger.error(
                f"Whatsapp Text Message {text} send to {user_session.phone} under {user_session.id} session Failed {e}"
            )
            return "failure"

    def sendButton(self, bot_instance_id, data_dict):
        logger.info("data_dict in sendButton: %s", data_dict)
        logger.info("bot instance id: %s", bot_instance_id)
        caption = "Select an option:"
        user_id = data_dict.get("user_id")
        bot_instance, user_session = _load_user_session(bot_instance_id, user_id)
        user_lang = user_session.user_config.get("language") or bot_instance.language or "hi"
        
        data = data_dict.get("menu", [])
        caption = "Select an option:"
        if data_dict.get("caption"):
            raw_caption = data_dict.get("caption")
            if isinstance(raw_caption, dict):
                caption = raw_caption.get(user_lang) or raw_caption.get("hi") or "Select an option:"
            else:
                caption = raw_caption
        elif data and len(data) > 0 and "caption" in data[0]:
            raw_caption = data[0]["caption"]
            if isinstance(raw_caption, dict):
                caption = raw_caption.get(user_lang) or raw_caption.get("hi") or "Select an option:"
            else:
                caption = raw_caption

        # Translate menu item labels
        translated_menu = []
        for item in data:
            new_item = item.copy()
            if isinstance(item.get("label"), dict):
                new_item["label"] = item["label"].get(user_lang) or item["label"].get("hi")
            translated_menu.append(new_item)
        data = translated_menu
        try:
            user_session.expected_response_type = "button"
            user_session.current_state = data_dict.get("state")

            # Handle SMJ object lookup with error handling
            smj_id = data_dict.get("smj_id")
            user_session.current_smj = bot_interface.models.SMJ.objects.get(id=smj_id)
            user_session.save()
            if len(data) > 3:
                print("in send_list msg ::")
                label = "Select Here"
                response = bot_interface.api.send_list_msg(
                    bot_instance_id=bot_instance_id,
                    contact_number=user_session.phone,
                    text=caption,
                    menu_list=data,
                    button_label=label,
                )

            elif len(data) <= 3 and ("description" in data[0]):
                label = "Select Here"
                response = bot_interface.api.send_list_msg(
                    bot_instance_id=bot_instance_id,
                    contact_number=user_session.phone,
                    text=caption,
                    menu_list=data,
                    button_label=label,
                )

            else:

                label = "Select Here"
                response = bot_interface.api.send_button_msg(
                    bot_instance_id=bot_instance_id,
                    contact_number=user_session.phone,
                    text=caption,
                    menu_list=data,
                )

            # Return success/failure based on API response
            if response and response.get("messages"):
                logger.info(
                    f"Menu Send to user {user_session.phone} under {user_session.id} session"
                )
                return "success"
            else:
                logger.error(
                    f"Failed to Send Menu to user {user_session.phone} under {user_session.id} session"
                )
                return "failure"

        except Exception as e:
            logger.error(f"Failed to Send Menu to user: {e}")
            return "failure"

    def sendLocationRequest(self, bot_instance_id, data_dict, text=None):
        user_id = data_dict.get("user_id")
        smj_id = data_dict.get("smj_id")
        user_session = self._load_user_session(bot_instance_id, user_id)
        user_session.expected_response_type = "location"
        user_session.current_state = data_dict.get("state")
        user_session.current_smj = bot_interface.models.SMJ.objects.get(id=smj_id)
        user_session.save()
        user_lang = user_session.user_config.get("language") or "hi"
        if not text:
            text = "कृपया स्थान भेजें"
        elif isinstance(text, dict):
            text = text.get(user_lang) or text.get("hi") or "कृपया स्थान भेजें"

        # Handle SMJ object lookup with error handling

        response = bot_interface.api.send_location_request(
            bot_instance_id=bot_instance_id,
            contact_number=user_session.phone,
            text=text,
        )

        # Return None to wait for user's actual response message
        if response and response.get("messages"):
            logger.info(f"Location message sent for session {user_session.id}")
            return None
        else:
            logger.error(f"Failed to send location message: {response}")
            return "failure"

    def sendCommunityByLocation(self, bot_instance_id, data_dict):
        """
        Send community options based on user's location.
        """
        logger.debug("sendCommunityByLocation called")

        try:
            user_id = data_dict.get("user_id")
            bot_instance, user_session = _load_user_session(bot_instance_id, user_id)

            data = data_dict.get("data", {})
            get_data_from = data.get("getDataFrom", {})

            state_name = get_data_from.get("state")
            field_name = get_data_from.get("field", "data")

            user_session.current_state = data_dict.get("state")

            latitude, longitude = _extract_lat_lon_from_session(
                user_session.current_session, state_name, field_name
            )

            if not latitude or not longitude:
                logger.error("Location data not found in user session")
                return "failure"

            # Set SMJ
            smj_id = data_dict.get("smj_id")
            user_session.current_smj = bot_interface.models.SMJ.objects.get(id=smj_id)

            from public_api.views import get_location_info_by_lat_lon
            # import requests  # Placeholder for external API call

            logger.debug("Fetching location info lat=%s lon=%s", latitude, longitude)
            raw_response = get_location_info_by_lat_lon(
                lat=float(latitude), lon=float(longitude)
            )

            location_data = _normalize_location_response(raw_response)
            if not location_data:
                return "no_communities"

            communities = get_communities(
                state_name=location_data.get("State", ""),
                district_name=location_data.get("District", ""),
                block_name=location_data.get("Block", ""),
            )

            if not communities or communities == "no_communities":
                return "no_communities"

            menu_list = [
                {
                    "value": c.get("community_id"),
                    "label": c.get("name"),
                    "description": c.get("description", ""),
                }
                for c in communities
            ]

            response = bot_interface.api.send_list_msg(
                bot_instance_id=bot_instance_id,
                contact_number=user_session.phone,
                text="कृपया अपना समुदाय चुनें",
                menu_list=menu_list,
            )

            user_session.expected_response_type = "community"
            user_session.save()

            return (
                "success" if response and response.get("messages") else "no_communities"
            )

        except Exception:
            logger.exception("Error in sendCommunityByLocation")
            return "no_communities"

    def sendStates(self, bot_instance_id, data_dict):
        logger.debug("sendStates called")

        try:
            user_id = data_dict.get("user_id")
            bot_instance, user_session = _load_user_session(bot_instance_id, user_id)

            user_session.current_state = data_dict.get("state")
            user_session.current_smj = bot_interface.models.SMJ.objects.get(
                id=data_dict.get("smj_id")
            )

            # import requests  # Placeholder for external API call
#             from geoadmin.models import State

            state_ids = (
                Location.objects.filter(communities__isnull=False)
                .values_list("state_id", flat=True)
                .distinct()
            )

            states = State.objects.filter(pk__in=state_ids).order_by("state_name")

            menu_list = [
                {"value": s.pk, "label": s.state_name, "description": ""}
                for s in states
            ]

            return _prepare_and_send_list(
                bot_instance_id,
                user_session,
                menu_list,
                text="कृपया अपना राज्य चुनें",
            )

        except Exception:
            logger.exception("Error in sendStates")
            return "failure"

    def sendDistricts(self, bot_instance_id, data_dict):
        logger.debug("sendDistricts called")

        try:
            user_id = data_dict.get("user_id")
            bot_instance, user_session = _load_user_session(bot_instance_id, user_id)

            user_session.current_state = data_dict.get("state")
            user_session.current_smj = bot_interface.models.SMJ.objects.get(
                id=data_dict.get("smj_id")
            )

            data = data_dict.get("data", {})
            get_data_from = data.get("getDataFrom", {})
            state_name = get_data_from.get("state")
            field = get_data_from.get("field", "data")

            if not state_name:
                logger.error("Missing getDataFrom state config")
                return "failure"

            # Extract state_id from session
            state_id = None
            for entry in user_session.current_session or []:
                if state_name in entry:
                    state_id = entry[state_name].get(field)
                    break

            if not state_id:
                logger.error("State ID not found in session")
                return "failure"

            response = requests.get(
                f"{CE_API_URL}get_districts_with_community/",
                params={"state_id": state_id},
                timeout=30,
            )
            response.raise_for_status()

            districts = response.json().get("data", [])
            if not districts:
                return "failure"

            menu_list = [
                {"value": d.get("id"), "label": d.get("name"), "description": ""}
                for d in districts
            ]

            return _prepare_and_send_list(
                bot_instance_id,
                user_session,
                menu_list,
                text="कृपया अपना जिला चुनें",
            )

        except Exception:
            logger.exception("Error in sendDistricts")
            return "failure"

    def sendCommunityByStateDistrict(self, bot_instance_id, data_dict):
        """
        Send community options based on selected state and district.
        """
        logger.debug("sendCommunityByStateDistrict called")

        try:
            user_id = data_dict.get("user_id")
            bot_instance, user_session = _load_user_session(bot_instance_id, user_id)

            # Update session context
            user_session.expected_response_type = "community"
            user_session.current_state = data_dict.get("state")
            user_session.current_smj = bot_interface.models.SMJ.objects.get(
                id=data_dict.get("smj_id")
            )

            data = data_dict.get("data", {})
            mappings = data.get("getDataFrom", [])

            if not mappings:
                logger.error("Missing getDataFrom configuration")
                return "failure"

            # Extract state & district IDs
            extracted = _extract_ids_from_session(
                user_session.current_session, mappings
            )

            state_id = extracted.get("SendState")
            district_id = extracted.get("SendDistrict")

            if not state_id or not district_id:
                logger.error(
                    "Missing required IDs | state_id=%s district_id=%s",
                    state_id,
                    district_id,
                )
                return "failure"

            # Call community API
            response = requests.get(
                f"{CE_API_URL}get_communities_by_location/",
                params={"state_id": state_id, "district_id": district_id},
                timeout=30,
            )
            response.raise_for_status()

            api_response = response.json()
            communities = (
                api_response.get("data") if api_response.get("success") else None
            )

            if not communities:
                logger.info(
                    "No communities found for state=%s district=%s",
                    state_id,
                    district_id,
                )
                return "failure"

            menu_list = [
                {
                    "value": c.get("community_id"),
                    "label": c.get("name"),
                    "description": c.get("description", ""),
                }
                for c in communities
            ]

            send_response = bot_interface.api.send_list_msg(
                bot_instance_id=bot_instance_id,
                contact_number=user_session.phone,
                text="कृपया अपना समुदाय चुनें",
                menu_list=menu_list,
            )

            user_session.save()

            return (
                "success"
                if send_response and send_response.get("messages")
                else "failure"
            )

        except Exception:
            logger.exception("Error in sendCommunityByStateDistrict")
            return "failure"

    def addUserToCommunity(self, bot_instance_id, data_dict):
        """
        Add user to a community.
        """
        logger.debug("addUserToCommunity called")

        try:
            user_id = data_dict.get("user_id")
            bot_instance, user_session = _load_user_session(bot_instance_id, user_id)

            # Update session
            user_session.expected_response_type = "button"
            user_session.current_state = data_dict.get("state")
            user_session.current_smj = bot_interface.models.SMJ.objects.get(
                id=data_dict.get("smj_id")
            )

            data = data_dict.get("data", {})
            mappings = data.get("getDataFrom")

            if not mappings:
                logger.error("Missing getDataFrom configuration")
                return "failure"

            if isinstance(mappings, dict):
                mappings = [mappings]

            community_id = _extract_community_id_from_session(
                user_session.current_session, mappings
            )

            if not community_id:
                logger.error("Community ID not found in session")
                return "failure"

            logger.info(
                "Adding user %s to community %s",
                user_session.phone,
                community_id,
            )

            response = requests.post(
                f"{CE_API_URL}add_user_to_community/",
                data={"community_id": community_id, "number": user_session.phone},
                timeout=30,
            )
            response.raise_for_status()
            api_response = response.json()

            if not api_response.get("success"):
                logger.error("Community API returned failure")
                return "failure"

            # Build & store community membership
            community_data = _build_community_data(user_id, community_id, api_response)

            bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)
            from bot_interface.utils import add_community_membership

            add_community_membership(bot_user, community_data)

            user_session.save()
            return "success"

        except Exception:
            logger.exception("Error in addUserToCommunity")
            return "failure"

    def get_user_communities(self, bot_instance_id, data_dict):
        """
        Determine whether user has single or multiple communities
        using API with DB fallback.
        """
        logger.debug("get_user_communities called")

        try:
            user_id = data_dict.get("user_id")
            bot_instance, user_session = _load_user_session(bot_instance_id, user_id)

            # Update session context
            user_session.current_state = data_dict.get("state")
            user_session.current_smj = bot_interface.models.SMJ.objects.get(
                id=data_dict.get("smj_id")
            )
            user_session.save()

            # Get phone number
            bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)
            phone_number = bot_user.user.contact_number

            logger.info("Fetching communities for phone=%s", phone_number)

            # Sync local cache (non-blocking)
            try:
                from bot_interface.utils import sync_community_data_from_database

                sync_community_data_from_database(bot_user)
            except Exception:
                logger.warning("Community sync failed (non-critical)", exc_info=True)

            communities = self._fetch_user_communities(phone_number)
            return self._determine_community_flow(communities, user_id)

        except Exception:
            logger.exception("Error in get_user_communities")
            return "failure"

    def _fetch_user_communities(self, phone_number: str) -> list:
        """
        Fetch user communities using API first, then DB fallback.
        """
        communities = self._get_communities_via_api(phone_number)

        if communities:
            return communities

        logger.info("API returned no communities, using DB fallback")
        return self._get_communities_via_database(phone_number)

    def _get_communities_via_api(self, phone_number: str) -> list:
        try:
            response = requests.get(
                f"{CE_API_URL}get_community_by_user/",
                params={"number": phone_number},
                timeout=10,
            )

            if response.status_code != 200:
                logger.warning("Community API returned status=%s", response.status_code)
                return []

            payload = response.json()
            return payload.get("data", []) if payload.get("success") else []

        except requests.exceptions.RequestException:
            logger.warning("Community API request failed", exc_info=True)
            return []

    def _get_communities_via_database(self, phone_number: str) -> list:
        try:
            # import requests  # Placeholder for external API call
            from users.models import User

            user = User.objects.filter(contact_number=phone_number).first()
            if not user:
                return []

            mappings = Community_user_mapping.objects.filter(user=user).select_related(
                "community", "community__project"
            )

            return [
                {
                    "community_id": m.community.id,
                    "community_name": (
                        m.community.project.name
                        if m.community.project
                        else f"Community {m.community.id}"
                    ),
                    "community_description": (
                        getattr(m.community.project, "description", "")
                        if m.community.project
                        else ""
                    ),
                    "organization": (
                        m.community.project.organization.name
                        if m.community.project and m.community.project.organization
                        else ""
                    ),
                    "created_at": (
                        m.created_at.isoformat() if hasattr(m, "created_at") else None
                    ),
                }
                for m in mappings
            ]

        except Exception:
            logger.exception("Database fallback failed")
            return []

    def _determine_community_flow(self, communities, user_id):
        """
        Determine community flow based on community count.
        Args:
            communities (list): List of user communities
            user_id (int): User ID for logging
        Returns:
            str: "single_community", "multiple_communities", or "failure"
        """
        community_count = len(communities)
        print(f"Determining flow for {community_count} communities for user {user_id}")

        if community_count == 1:
            print("User has single community")
            return "single_community"
        elif community_count > 1:
            print("User has multiple communities")
            return "multiple_communities"
        else:
            print(
                "User has no communities - this shouldn't happen in community features flow"
            )
            return "failure"

    def display_community_message(self, bot_instance_id, data_dict, mode="single"):
        """
        Display welcome message for users with single or multiple communities.

        Args:
            bot_instance_id (int): Bot instance ID
            data_dict (dict): User/session data
            mode (str): "single" or "multiple"

        Returns:
            str: "success" or "failure"
        """
        try:
            bot_instance = bot_interface.models.Bot.objects.get(id=bot_instance_id)
            user_id = data_dict.get("user_id")

            # Get user session
            user = bot_interface.models.UserSessions.objects.get(
                user=user_id, bot=bot_instance
            )

            # Get user communities
            bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)
            communities = bot_user.user_misc.get("community_membership", {}).get(
                "current_communities", []
            )

            if not communities:
                return "failure"

            # Resolve community name
            community_name = communities[0].get("community_name", "Unknown Community")

            if mode == "multiple":
                success, api_response = (
                    bot_interface.utils.check_user_community_status_http(user.phone)
                )
                if success and api_response.get("success"):
                    data = api_response.get("data", {})
                    last_id = data.get("misc", {}).get("last_accessed_community_id")

                    for c in data.get("data", []):
                        if c.get("community_id") == last_id:
                            community_name = c.get("name", community_name)
                            break

                welcome_text = (
                    f"🏠 आपने पिछली बार {community_name} समुदाय का उपयोग किया था।"
                )
            else:
                welcome_text = (
                    f"🏠 आप {community_name} समुदाय का हिस्सा हैं।\n\n"
                    "आप कैसे आगे बढ़ना चाहेंगे?"
                )

            response = bot_interface.api.send_text(
                bot_instance_id=bot_instance_id,
                contact_number=user.phone,
                text=welcome_text,
            )

            return "success" if response and response.get("messages") else "failure"

        except Exception:
            logger.exception("display_community_message failed")
            return "failure"

    def generate_community_menu(self, bot_instance_id, data_dict):
        """
        Generate dynamic menu from user's communities.
        """
        try:
            user_id = data_dict.get("user_id")
            bot_instance, user = _load_user_session(bot_instance_id, user_id)

            # Update session state
            user.expected_response_type = "button"
            user.current_state = data_dict.get("state")
            user.current_smj = bot_interface.models.SMJ.objects.get(
                id=data_dict.get("smj_id")
            )
            user.save()

            # Get user communities from misc
            bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)
            current_communities = bot_user.user_misc.get(
                "community_membership", {}
            ).get("current_communities", [])

            if not current_communities:
                return "failure"

            # Try API for last accessed community
            menu_items = []
            success, api_response = (
                bot_interface.utils.check_user_community_status_http(user.phone)
            )

            if success and api_response.get("success"):
                data = api_response.get("data", {})
                last_accessed_id = data.get("misc", {}).get(
                    "last_accessed_community_id"
                )
                api_communities = data.get("data", [])

                menu_items = [
                    {
                        "value": f"community_{c.get('community_id')}",
                        "label": c.get("name", "Unknown Community"),
                        "description": f"Select {c.get('name', 'Unknown Community')}",
                    }
                    for c in api_communities
                    if c.get("community_id") != last_accessed_id
                ]
            else:
                # Fallback: use stored communities
                menu_items = [
                    {
                        "value": f"community_{c.get('community_id')}",
                        "label": c.get("community_name", "Unknown Community"),
                        "description": f"Select {c.get('community_name', 'Unknown Community')}",
                    }
                    for c in current_communities
                ]

            # Add continue option
            menu_items.append(
                {
                    "value": "continue_last_accessed",
                    "label": "पिछला समुदाय चुनें",
                    "description": "अपने पिछले समुदाय के साथ वापस जाएं",
                }
            )

            # Send WhatsApp list
            response = bot_interface.api.send_list_msg(
                bot_instance_id=bot_instance_id,
                contact_number=user.phone,
                text="कृपया अपना समुदाय चुनें:",
                menu_list=menu_items,
                button_label="समुदाय चुनें",
            )

            return "success" if response and response.get("messages") else "failure"

        except Exception:
            logger.exception("generate_community_menu failed")
            return "failure"

    def store_active_community_and_context(self, bot_instance_id, data_dict):
        """
        Store active / selected community and navigation context.
        Handles:
        - single community auto-continue
        - last accessed community
        - menu-based community selection
        """
        try:
            user_id = data_dict.get("user_id")
            bot_instance = bot_interface.models.Bot.objects.get(id=bot_instance_id)

            user = bot_interface.models.UserSessions.objects.get(
                user=user_id, bot=bot_instance
            )

            event = data_dict.get("event") or data_dict.get("event_data", {}).get(
                "misc", ""
            )

            event_data = data_dict.get("event_data", {})

            # Explicit navigation events
            if event == "join_new":
                return "join_new"

            if event == "choose_other":
                return "choose_other"

            # Resolve community
            community_id = _resolve_community_id(user, user_id, event, event_data)

            if not community_id:
                return "failure"

            # Store context
            user.misc_data = user.misc_data or {}
            user.misc_data.update(
                {
                    "active_community_id": str(community_id),
                    "navigation_context": (
                        "community_selection"
                        if event_data.get("type") == "button"
                        else "auto_continue"
                    ),
                    "last_service_event": event,
                }
            )

            user.save()

            return "community_selected" if event_data.get("type") == "button" else event

        except Exception as e:
            logger.exception("store_active_community_and_context failed")
            logger.debug(str(e))

            return "failure"

    def display_service_menu_message(self, bot_instance_id, data_dict):
        """
        Display contextual service menu message.
        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Dictionary containing user and session data.
        Returns:
            str: "success" or "failure"
        """
        print("in display_service_menu_message")

        try:

            user_id = data_dict.get("user_id")
            bot_instance, user = _load_user_session(bot_instance_id, user_id)
            active_community_id = (
                user.misc_data.get("active_community_id") if user.misc_data else None
            )

            if active_community_id:
                # Get BotUsers object to find community name
                bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)
                current_communities = bot_user.user_misc.get(
                    "community_membership", {}
                ).get("current_communities", [])

                # Find the active community name
                community_name = "आपके समुदाय"  # Default fallback
                for community in current_communities:
                    if str(community.get("community_id")) == str(active_community_id):
                        community_name = community.get("community_name", community_name)
                        break

                # Create contextual service menu message
                service_text = f"📋 {community_name} के लिए सेवाएं\n\nआप क्या करना चाहते हैं:"
            else:
                # Fallback message if no active community
                service_text = "📋 समुदाय सेवाएं\n\nआप क्या करना चाहते हैं:"

            # Send service menu message
            response = bot_interface.api.send_text(
                bot_instance_id=bot_instance_id,
                contact_number=user.phone,
                text=service_text,
            )

            print(f"Service menu message sent: {response}")

            if response and response.get("messages"):
                return "success"
            else:
                return "failure"

        except Exception as e:
            print(f"Error in display_service_menu_message: {e}")
            return "failure"

    def handle_service_selection(self, bot_instance_id, data_dict):
        """
        Handle back navigation based on stored context.
        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Dictionary containing user and session data.
        Returns:
            str: "back_from_single", "back_from_multiple", "back_from_selection", or event passed through
        """
        print("in handle_service_selection")

        try:
            bot_instance = bot_interface.models.Bot.objects.get(id=bot_instance_id)
            user_id = data_dict.get("user_id")

            # Get user session
            user = bot_interface.models.UserSessions.objects.get(
                user=user_id, bot=bot_instance
            )

            # Get the event
            event = data_dict.get("event", "")
            print(f"Handling service selection for event: {event}")

            # For all events (work_demand, grievance, exit_session, etc.), pass through the event
            return event

        except Exception as e:
            print(f"Error in handle_service_selection: {e}")
            return "failure"

    def store_location_data(self, bot_instance_id, data_dict):
        """
        Store location data from WhatsApp location event into work_demand context.
        """
        try:
            user = _get_user_session(bot_instance_id, data_dict.get("user_id"))
            if not user:
                return "failure"

            location_data = _extract_location_data(user, data_dict, force_fresh=True)

            if not location_data:
                print("No location data found")
                return "failure"
            smj = _get_smj(data_dict.get("smj_id"))

            flow_type = getattr(smj, "name", None)
            user.misc_data = user.misc_data or {}
            user.misc_data.setdefault(flow_type, {})
            user.misc_data[flow_type]["location"] = location_data
            user.save()

            print(f"Stored location data: {location_data}")
            return "success"

        except Exception:
            logger.exception("store_location_data failed")
            return "failure"

    def store_audio_data(self, bot_instance_id, data_dict):
        return self._store_media_data(
            bot_instance_id=bot_instance_id, data_dict=data_dict, media_type="audio"
        )

    def store_photo_data(self, bot_instance_id, data_dict, flow_type="work_demand"):
        return self._store_media_data(
            bot_instance_id=bot_instance_id, data_dict=data_dict, media_type="photo"
        )

    def archive_and_end_session(self, bot_instance_id, data_dict):
        """
        Archive current session and end it completely.
        """
        try:
            user = _get_user_session(bot_instance_id, data_dict.get("user_id"))
            if not user:
                return "failure"

            _archive_user_session(user, reason="work_demand_completion")
            _reset_user_session(user)

            return "success"

        except Exception:
            logger.exception("archive_and_end_session failed")
            return "failure"

    def _extract_flow_data(self, user, flow_type):
        data = user.misc_data.get(flow_type, {}) if user.misc_data else {}

        if "photos" in data:
            data["photos_note"] = (
                "Photo paths are HDPI processed images from WhatsApp media"
            )

        return data

    def _build_community_context(self, user):
        active_community_id = (
            user.misc_data.get("active_community_id") if user.misc_data else None
        )
        if not active_community_id:
            return {}

        try:
            # import requests  # Placeholder for external API call

            community = Community.objects.get(id=active_community_id)
            context = {
                "community_id": active_community_id,
                "community_name": (
                    community.project.name if community.project else "Unknown"
                ),
                "organization": (
                    community.project.organization.name
                    if community.project and community.project.organization
                    else "Unknown"
                ),
                "location_hierarchy": {},
            }

            for loc in community.locations.all():
                if loc.state:
                    context["location_hierarchy"]["state"] = loc.state.state_name
                if loc.district:
                    context["location_hierarchy"][
                        "district"
                    ] = loc.district.district_name
                if loc.block:
                    context["location_hierarchy"]["block"] = loc.block.block_name

            return context

        except Exception as e:
            logger.warning(f"Community context load failed: {e}")
            return {"community_id": active_community_id, "error": "load_failed"}

    def _build_community_context(self, user):
        active_community_id = (
            user.misc_data.get("active_community_id") if user.misc_data else None
        )
        if not active_community_id:
            return {}

        try:
            # import requests  # Placeholder for external API call

            community = Community.objects.get(id=active_community_id)
            context = {
                "community_id": active_community_id,
                "community_name": (
                    community.project.name if community.project else "Unknown"
                ),
                "organization": (
                    community.project.organization.name
                    if community.project and community.project.organization
                    else "Unknown"
                ),
                "location_hierarchy": {},
            }

            for loc in community.locations.all():
                if loc.state:
                    context["location_hierarchy"]["state"] = loc.state.state_name
                if loc.district:
                    context["location_hierarchy"][
                        "district"
                    ] = loc.district.district_name
                if loc.block:
                    context["location_hierarchy"]["block"] = loc.block.block_name

            return context

        except Exception as e:
            logger.warning(f"Community context load failed: {e}")
            return {"community_id": active_community_id, "error": "load_failed"}

    def _log_flow_completion(self, bot_instance_id, data_dict, item_type=None):
        smj = _get_smj(data_dict.get("smj_id"))
        flow_type = getattr(smj, "name", None)
        logger.info(f"Runnuing for flow type: {flow_type}")
        """
        Generic logger for work_demand / story / grievance flows.
        Creates UserLogs ONLY at the end, reading accumulated data
        from user.misc_data.
        """
        try:
            # ----------------------------
            # Load core objects
            # ----------------------------
            bot_instance = _get_bot_instance(bot_instance_id)
            if not bot_instance:
                return "failure"

            user = _get_user_session(bot_instance, data_dict.get("user_id"))
            print(f"User Session Data: {user.__dict__}")
            if not user:
                return "failure"

            bot_user = _get_bot_user(user.user_id)
            smj = _get_smj(data_dict.get("smj_id"))

            # ----------------------------
            # CRITICAL: Refresh user cache
            # ----------------------------
            user.refresh_from_db(fields=["misc_data"])
            print(user.__dict__)
            # ----------------------------
            # Find correct flow cache
            # (defensive against key mismatch)
            # ----------------------------
            flow_cache = {}
            misc_data = user.misc_data or {}

            if flow_type in misc_data:
                flow_cache = misc_data.get(flow_type, {})
            else:
                # fallback: partial match (debug safety)
                for key, value in misc_data.items():
                    if flow_type in key:
                        flow_cache = value
                        break

            # ----------------------------
            # Extract accumulated media
            # ----------------------------
            audio_data = flow_cache.get("audio")
            photo_data = flow_cache.get("photos")
            community_id = flow_cache.get("community_id")

            # ----------------------------
            # Extract structured flow data
            # ----------------------------
            flow_data = self._extract_flow_data(user, flow_type)

            # ----------------------------
            # Build community context
            # ----------------------------
            community_context = self._build_community_context(user)
            if community_id and isinstance(community_context, dict):
                community_context.setdefault("community_id", community_id)

            # ----------------------------
            # Build misc payload
            # ----------------------------
            misc_payload = _build_misc_payload(
                self,
                flow_type=flow_type,
                flow_data=flow_data,
                community_context=community_context,
                bot_instance=bot_instance,
                user=user,
                bot_user=bot_user,
            )

            misc_payload["audio_data"] = audio_data
            misc_payload["photo_data"] = photo_data

            # ----------------------------
            # Create final UserLog
            # ----------------------------
            bot_interface.models.UserLogs.objects.create(
                app_type=user.app_type,
                bot=bot_instance,
                user=bot_user,
                key1="useraction",
                value1=flow_type,
                misc=misc_payload,
                smj=smj,
            )

            # ----------------------------
            # EXTERNAL API CALL: upsert_item
            # ----------------------------
            try:
                external_api_url = "http://127.0.0.1:8001/api/v1/upsert_item/"

                # Coordinates
                location = flow_cache.get("location", {})
                lat = location.get("latitude")
                lon = location.get("longitude")
                coordinates_json = json.dumps({"lat": lat, "lon": lon})

                # Payload enrichment from BotUser
                user_misc = bot_user.user_misc or {}
                community_id = user_misc.get("community_id")
                village_name = user_misc.get("village_name", "Unknown Village")
                plan_id = user_misc.get("plan_id")
                plan_name = user_misc.get("plan_name")

                # Payload
                phone_number = user.phone
                if phone_number:
                    phone_number = phone_number.replace("+", "").replace("-", "").replace(" ", "")
                if phone_number and not phone_number.startswith("91"):
                    phone_number = f"91{phone_number}"

                # Map item_type for backend compatibility
                final_item_type = item_type
                if item_type == "ASSET_DEMAND":
                    final_item_type = "WORK_DEMAND"

                payload = {
                    "coordinates": coordinates_json,
                    "item_type": final_item_type or "WORK_DEMAND",
                    "community_id": community_id or 1,  # Default to 1 if missing
                    "number": phone_number,
                    "source": "BOT",
                    "bot_id": bot_instance_id,
                    "misc": json.dumps({
                        "village_name": village_name,
                        "plan_id": plan_id,
                        "plan_name": plan_name
                    }),
                }

                # Files collection
                files_payload = []

                # Handle Audio (Plural field: 'audios')
                if audio_data and audio_data.get("local_path"):
                    audio_path = audio_data["local_path"]
                    if os.path.exists(audio_path):
                        files_payload.append(
                            ("audios", open(audio_path, "rb"))
                        )

                # Handle Photos (Plural field: 'images')
                if photo_data:
                    photos_list = (
                        photo_data if isinstance(photo_data, list) else [photo_data]
                    )
                    for photo in photos_list:
                        if photo.get("local_path") and os.path.exists(photo["local_path"]):
                            files_payload.append(
                                ("images", open(photo["local_path"], "rb"))
                            )

                logger.info(
                    f"Calling upsert API {external_api_url} for {final_item_type} (Community: {community_id})"
                )
                ext_response = requests.post(
                    external_api_url, data=payload, files=files_payload, timeout=30
                )
                logger.info(
                    f"External API Response: {ext_response.status_code} | {ext_response.text}"
                )

                # Close file handles
                for _, f in files_payload:
                    f.close()

            except Exception as e:
                logger.error(f"External API call for create_village_item failed: {e}")

            # ----------------------------
            # Cleanup cached flow data
            # ----------------------------
            if flow_type in misc_data:
                misc_data.pop(flow_type, None)
                user.misc_data = misc_data
                user.save(update_fields=["misc_data"])

            return "success"

        except Exception:
            logger.exception(f"log_{flow_type}_completion failed")
            return "failure"

    def log_work_demand_completion(self, bot_instance_id, data_dict):
        print(f"Data Dict for work demand {data_dict}")
        return self._log_flow_completion(
            bot_instance_id=bot_instance_id,
            data_dict=data_dict,
            item_type="ASSET_DEMAND",
        )

    def log_story_completion(self, bot_instance_id, data_dict):
        return self._log_flow_completion(
            bot_instance_id=bot_instance_id, data_dict=data_dict, item_type="STORY"
        )

    def _extract_community_id_for_join(self, user_session, event_data):
        """
        Extract community_id from session or button event.
        """
        import json

        current_session = user_session.current_session

        try:
            if isinstance(current_session, str):
                current_session = json.loads(current_session or "[]")
        except Exception:
            current_session = []

        # 1️⃣ Prefer session-based selection
        for entry in current_session or []:
            if not isinstance(entry, dict):
                continue

            if "CommunityByStateDistrict" in entry:
                return entry["CommunityByStateDistrict"].get("misc")

            if "CommunityByLocation" in entry:
                return entry["CommunityByLocation"].get("misc")

        # 2️⃣ Fallback: button click
        if event_data.get("type") == "button":
            return event_data.get("misc") or event_data.get("data")

        return None

    def _join_user_to_community(self, user_session, community_id, phone_number):
        """
        Calls CE API to join user to community and stores context locally.
        """
        try:
            response = requests.post(
                url=f"{CE_API_URL}add_user_to_community/",
                data={
                    "community_id": community_id,
                    "number": int(phone_number),
                },
                timeout=30,
            )
            response.raise_for_status()
            api_response = response.json()

            if not api_response.get("success"):
                logger.warning(f"Community join failed: {api_response}")
                return "failure"

            if not user_session.misc_data:
                user_session.misc_data = {}

            user_session.misc_data.update(
                {
                    "active_community_id": community_id,
                    "navigation_context": "join_community",
                    "join_timestamp": timezone.now().isoformat(),
                }
            )
            user_session.save()

            return "success"

        except Exception:
            logger.exception("Community join API failed")
            return "failure"

    def add_user_to_selected_community_join_flow(self, bot_instance_id, data_dict):
        """
        Add user to selected community in join community flow.
        Extracts community ID from:
          1. Session data (CommunityByStateDistrict / CommunityByLocation)
          2. Button event fallback
        """
        try:
            bot_instance = bot_interface.models.Bot.objects.get(id=bot_instance_id)
            user_id = data_dict.get("user_id")

            user_session = bot_interface.models.UserSessions.objects.get(
                user=user_id, bot=bot_instance
            )
            bot_user = user_session.user

            community_id = self._extract_community_id_for_join(
                user_session=user_session,
                event_data=data_dict.get("event_data", {}),
            )

            if not community_id:
                logger.warning("Community ID not found for join flow")
                return "failure"

            return self._join_user_to_community(
                user_session=user_session,
                community_id=community_id,
                phone_number=bot_user.user.contact_number,
            )

        except Exception:
            logger.exception("add_user_to_selected_community_join_flow failed")
            return "failure"

    def send_join_success_message(self, bot_instance_id, data_dict):
        """
        Send success message after joining new community.
        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Dictionary containing user and session data.
        Returns:
            str: "success" or "failure"
        """
        print(f"DEBUG: send_join_success_message called")

        try:
            bot_instance = bot_interface.models.Bot.objects.get(id=bot_instance_id)
            user_id = data_dict.get("user_id")

            # Get user session
            user_session = bot_interface.models.UserSessions.objects.get(
                user=user_id, bot=bot_instance
            )
            bot_user = user_session.user

            # Get community name from misc_data
            community_id = (
                user_session.misc_data.get("active_community_id")
                if user_session.misc_data
                else None
            )
            community_name = "the community"

            if community_id:
                try:
                    # Try to get community name from existing patterns
                    # import requests  # Placeholder for external API call

                    community = Community.objects.get(id=community_id)
                    community_name = community.project
                except:
                    pass

            # Prepare success message
            success_text = f"✅ बहुत बढ़िया! आप सफलतापूर्वक {community_name} में शामिल हो गए हैं। अब आप समुदायिक सेवाओं का उपयोग कर सकते हैं।"

            # Send the message using bot_interface.api.send_text directly
            user_phone = bot_user.user.contact_number
            response = bot_interface.api.send_text(
                bot_instance_id=bot_instance_id,
                contact_number=user_phone,
                text=success_text,
            )

            print(f"DEBUG: Join success message sent: {response}")
            return "success"

        except Exception as e:
            print(f"DEBUG: Exception in send_join_success_message: {e}")
            import traceback

            traceback.print_exc()
            return "failure"

    def return_to_community_services(self, bot_instance_id, data_dict):
        """
        Prepare return to community services menu after joining new community.
        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Dictionary containing user and session data.
        Returns:
            str: "success" or "failure"
        """
        print(f"DEBUG: return_to_community_services called")

        try:
            # Prepare SMJ jump back to community features
            jump_data = {
                "_smj_jump": {
                    "smj_name": "community_features",
                    "smj_id": 6,  # Assuming community features SMJ ID is 6
                    "init_state": "ServiceMenu",
                    "states": [],  # Will be loaded from SMJ
                }
            }

            # Add jump data to data_dict for store_active_community_and_context processing
            data_dict.update(jump_data)

            print(f"DEBUG: Prepared return to community services: {jump_data}")
            return "success"

        except Exception as e:
            print(f"DEBUG: Exception in return_to_community_services: {e}")
            import traceback

            traceback.print_exc()
            return "failure"

    def process_and_submit_work_demand(self, user_log_id):
        """
        Processes work demand data from UserLogs and submits to Community Engagement API.

        Args:
            user_log_id (int): ID of the UserLogs record containing work demand data

        Returns:
            dict: API response from upsert_item endpoint or error dict
        """
        import requests
        import json
        import os
        from django.conf import settings
        from bot_interface.models import UserLogs

        print(f"invoking proces and submit work demand")
        try:
            # Get the UserLogs record
            try:
                user_log = UserLogs.objects.get(id=user_log_id)
            except UserLogs.DoesNotExist:
                print("user log not found")
                return {
                    "success": False,
                    "message": f"UserLogs record with id {user_log_id} not found",
                }

            # Extract work demand data from misc field
            work_demand_data = user_log.misc.get("work_demand_data", {})
            if not work_demand_data:
                # Try alternative key structure
                work_demand_data = user_log.misc.get("work_demand", {})
            print(f"workd demand data {work_demand_data}")
            if not work_demand_data:
                return {
                    "success": False,
                    "message": "No work demand data found in UserLogs.misc",
                }

            print(f"Processing work demand data: {work_demand_data}")

            # Get user's community context from UserLogs misc data
            community_id = None
            try:
                # Get community_id from community_context in the UserLogs misc field
                if "community_context" in user_log.misc:
                    community_context = user_log.misc["community_context"]
                    community_id = community_context.get("community_id")
                    print(f"Found community_id in UserLogs: {community_id}")

                if not community_id:
                    return {
                        "success": False,
                        "message": "Could not find community_id in UserLogs data",
                    }

            except Exception as e:
                print(f"Error getting community context from UserLogs: {e}")
                return {
                    "success": False,
                    "message": f"Error getting community context: {e}",
                }

            # Prepare files for upload from local filesystem
            files = {}

            # Handle audio file - use "audios" key for API
            if "audio" in work_demand_data:
                audio_path = work_demand_data["audio"]
                if audio_path and os.path.exists(audio_path):
                    try:
                        with open(audio_path, "rb") as audio_file:
                            audio_content = audio_file.read()
                            # Determine file extension
                            file_ext = os.path.splitext(audio_path)[1] or ".ogg"
                            mime_type = (
                                "audio/ogg" if file_ext == ".ogg" else "audio/mpeg"
                            )
                            files["audios"] = (
                                f"audio{file_ext}",
                                audio_content,
                                mime_type,
                            )
                            print(f"Added audio file: {audio_path}")
                    except Exception as e:
                        print(f"Error reading audio file {audio_path}: {e}")
                else:
                    print(f"Audio file not found or invalid path: {audio_path}")

            # Handle photo files - use indexed keys for multiple images
            if "photos" in work_demand_data and isinstance(
                work_demand_data["photos"], list
            ):
                for i, photo_path in enumerate(work_demand_data["photos"]):
                    if photo_path and os.path.exists(photo_path):
                        try:
                            with open(photo_path, "rb") as photo_file:
                                photo_content = photo_file.read()
                                # Determine file extension
                                file_ext = os.path.splitext(photo_path)[1] or ".jpg"
                                mime_type = (
                                    "image/jpeg"
                                    if file_ext.lower() in [".jpg", ".jpeg"]
                                    else "image/png"
                                )
                                files[f"images_{i}"] = (
                                    f"photo_{i}{file_ext}",
                                    photo_content,
                                    mime_type,
                                )
                                print(f"Added photo file {i}: {photo_path}")
                        except Exception as e:
                            print(f"Error reading photo file {photo_path}: {e}")
                    else:
                        print(f"Photo file not found or invalid path: {photo_path}")

            # Prepare coordinates from location data - use lat/lon format
            coordinates = {}
            if "location" in work_demand_data:
                location = work_demand_data["location"]
                if isinstance(location, dict):
                    coordinates = {
                        "lat": location.get("latitude"),
                        "lon": location.get("longitude"),
                    }
                    # Only include if both lat and lon are available
                    if not (coordinates["lat"] and coordinates["lon"]):
                        coordinates = {}

            # Get user contact number through proper relationship chain
            try:
                # UserLogs.user_id -> BotUsers.id -> BotUsers.user_id -> Users.id -> Users.contact_number
                bot_user = user_log.user  # This is BotUsers instance
                actual_user = bot_user.user  # This is Users instance
                contact_number = actual_user.contact_number

                if not contact_number:
                    return {
                        "success": False,
                        "message": "Could not get user contact number",
                    }

            except AttributeError as e:
                return {
                    "success": False,
                    "message": f"Could not get user contact number from relationship chain: {e}",
                }

            # Prepare API payload
            payload = {
                "item_type": "Asset_Demand",
                "coordinates": json.dumps(coordinates) if coordinates else "",
                "number": contact_number,
                "community_id": community_id,
                "source": "BOT",
                "bot_id": user_log.bot.id,
                "title": f"Asset_Demand",  # Auto-generated if not provided
                "transcript": work_demand_data.get(
                    "description", ""
                ),  # If any description exists
            }

            print(f"API Payload: {payload}")
            print(f"Files to upload: {list(files.keys())}")

            # Submit to Community Engagement API
            api_url = f"{CE_API_URL}upsert_item/"

            try:
                response = requests.post(
                    api_url, data=payload, files=files, timeout=30  # 30 second timeout
                )

                print(f"API Response Status: {response.status_code}")
                print(f"API Response: {response.text}")

                if response.status_code == 200 or response.status_code == 201:
                    result = response.json()
                    if result.get("success"):
                        print(
                            f"Successfully submitted work demand. Item ID: {result.get('item_id')}"
                        )

                        # Update UserLogs with success status
                        user_log.value2 = "success"
                        user_log.value3 = "0"  # No retries needed
                        user_log.key4 = "response"
                        user_log.value4 = response.text
                        user_log.save()
                        print(f"Updated UserLogs ID {user_log.id} with success status")

                        return result
                    else:
                        print(f"API returned success=False: {result}")

                        # Update UserLogs with API failure status
                        user_log.value2 = "failure"
                        user_log.value3 = "0"
                        user_log.key4 = "response"
                        user_log.value4 = response.text
                        user_log.save()
                        print(
                            f"Updated UserLogs ID {user_log.id} with API failure status"
                        )

                        return result
                else:
                    # Update UserLogs with HTTP error status
                    user_log.value2 = "failure"
                    user_log.value3 = "0"
                    user_log.key4 = "error"
                    user_log.value4 = f"HTTP {response.status_code}: {response.text}"
                    user_log.save()
                    print(f"Updated UserLogs ID {user_log.id} with HTTP error status")

                    return {
                        "success": False,
                        "message": f"API call failed with status {response.status_code}: {response.text}",
                    }

            except requests.exceptions.RequestException as e:
                print(f"Request error: {e}")

                # Update UserLogs with request error status
                user_log.value2 = "failure"
                user_log.value3 = "0"
                user_log.key4 = "error"
                user_log.value4 = f"Request error: {e}"
                user_log.save()
                print(f"Updated UserLogs ID {user_log.id} with request error status")

                return {"success": False, "message": f"Request error: {e}"}

        except Exception as e:
            print(f"Error in process_and_submit_work_demand: {e}")
            import traceback

            traceback.print_exc()

            # Update UserLogs with internal error status
            try:
                user_log.value2 = "failure"
                user_log.value3 = "0"
                user_log.key4 = "error"
                user_log.value4 = f"Internal error: {e}"
                user_log.save()
                print(f"Updated UserLogs ID {user_log.id} with internal error status")
            except Exception as save_error:
                print(f"Failed to update UserLogs: {save_error}")

            return {"success": False, "message": f"Internal error: {e}"}

    def process_and_submit_story(self, user_log_id):
        """
        Processes work demand data from UserLogs and submits to Community Engagement API.

        Args:
            user_log_id (int): ID of the UserLogs record containing work demand data

        Returns:
            dict: API response from upsert_item endpoint or error dict
        """
        import requests
        import json
        import os
        from django.conf import settings
        from bot_interface.models import UserLogs

        print(f"invoking proces and submit story")
        try:
            # Get the UserLogs record
            try:
                user_log = UserLogs.objects.get(id=user_log_id)
            except UserLogs.DoesNotExist:
                return {
                    "success": False,
                    "message": f"UserLogs record with id {user_log_id} not found",
                }

            # Extract work demand data from misc field
            story_data = user_log.misc.get("story_data", {})
            if not story_data:
                # Try alternative key structure
                work_demand_data = user_log.misc.get("story", {})
            print(f"story data {story_data}")
            if not story_data:
                return {
                    "success": False,
                    "message": "No work demand data found in UserLogs.misc",
                }

            print(f"Processing work demand data: {story_data}")

            # Get user's community context from UserLogs misc data
            community_id = None
            try:
                # Get community_id from community_context in the UserLogs misc field
                if "community_context" in user_log.misc:
                    community_context = user_log.misc["community_context"]
                    community_id = community_context.get("community_id")
                    print(f"Found community_id in UserLogs: {community_id}")

                if not community_id:
                    return {
                        "success": False,
                        "message": "Could not find community_id in UserLogs data",
                    }

            except Exception as e:
                print(f"Error getting community context from UserLogs: {e}")
                return {
                    "success": False,
                    "message": f"Error getting community context: {e}",
                }

            # Prepare files for upload from local filesystem
            files = {}

            # Handle audio file - use "audios" key for API
            if "audio" in story_data:
                audio_path = story_data["audio"]
                if audio_path and os.path.exists(audio_path):
                    try:
                        with open(audio_path, "rb") as audio_file:
                            audio_content = audio_file.read()
                            # Determine file extension
                            file_ext = os.path.splitext(audio_path)[1] or ".ogg"
                            mime_type = (
                                "audio/ogg" if file_ext == ".ogg" else "audio/mpeg"
                            )
                            files["audios"] = (
                                f"audio{file_ext}",
                                audio_content,
                                mime_type,
                            )
                            print(f"Added audio file: {audio_path}")
                    except Exception as e:
                        print(f"Error reading audio file {audio_path}: {e}")
                else:
                    print(f"Audio file not found or invalid path: {audio_path}")

            # Handle photo files - use indexed keys for multiple images
            if "photos" in story_data and isinstance(story_data["photos"], list):
                for i, photo_path in enumerate(story_data["photos"]):
                    if photo_path and os.path.exists(photo_path):
                        try:
                            with open(photo_path, "rb") as photo_file:
                                photo_content = photo_file.read()
                                # Determine file extension
                                file_ext = os.path.splitext(photo_path)[1] or ".jpg"
                                mime_type = (
                                    "image/jpeg"
                                    if file_ext.lower() in [".jpg", ".jpeg"]
                                    else "image/png"
                                )
                                files[f"images_{i}"] = (
                                    f"photo_{i}{file_ext}",
                                    photo_content,
                                    mime_type,
                                )
                                print(f"Added photo file {i}: {photo_path}")
                        except Exception as e:
                            print(f"Error reading photo file {photo_path}: {e}")
                    else:
                        print(f"Photo file not found or invalid path: {photo_path}")

            # Prepare coordinates from location data - use lat/lon format
            coordinates = {}
            if "location" in story_data:
                location = story_data["location"]
                if isinstance(location, dict):
                    coordinates = {
                        "lat": location.get("latitude"),
                        "lon": location.get("longitude"),
                    }
                    # Only include if both lat and lon are available
                    if not (coordinates["lat"] and coordinates["lon"]):
                        coordinates = {}

            # Get user contact number through proper relationship chain
            try:
                # UserLogs.user_id -> BotUsers.id -> BotUsers.user_id -> Users.id -> Users.contact_number
                bot_user = user_log.user  # This is BotUsers instance
                actual_user = bot_user.user  # This is Users instance
                contact_number = actual_user.contact_number

                if not contact_number:
                    return {
                        "success": False,
                        "message": "Could not get user contact number",
                    }

            except AttributeError as e:
                return {
                    "success": False,
                    "message": f"Could not get user contact number from relationship chain: {e}",
                }

            # Prepare API payload
            payload = {
                "item_type": "Story",
                "coordinates": json.dumps(coordinates) if coordinates else "",
                "number": contact_number,
                "community_id": community_id,
                "source": "BOT",
                "bot_id": user_log.bot.id,
                "title": "Story",  # Auto-generated if not provided
                "transcript": story_data.get(
                    "description", ""
                ),  # If any description exists
            }

            print(f"API Payload: {payload}")
            print(f"Files to upload: {list(files.keys())}")

            # Submit to Community Engagement API
            api_url = f"{CE_API_URL}upsert_item/"

            try:
                response = requests.post(
                    api_url, data=payload, files=files, timeout=30  # 30 second timeout
                )

                print(f"API Response Status: {response.status_code}")
                print(f"API Response: {response.text}")

                if response.status_code == 200 or response.status_code == 201:
                    result = response.json()
                    if result.get("success"):
                        print(
                            f"Successfully submitted work demand. Item ID: {result.get('item_id')}"
                        )

                        # Update UserLogs with success status
                        user_log.value2 = "success"
                        user_log.value3 = "0"  # No retries needed
                        user_log.key4 = "response"
                        user_log.value4 = response.text
                        user_log.save()
                        print(f"Updated UserLogs ID {user_log.id} with success status")

                        return result
                    else:
                        print(f"API returned success=False: {result}")

                        # Update UserLogs with API failure status
                        user_log.value2 = "failure"
                        user_log.value3 = "0"
                        user_log.key4 = "response"
                        user_log.value4 = response.text
                        user_log.save()
                        print(
                            f"Updated UserLogs ID {user_log.id} with API failure status"
                        )

                        return result
                else:
                    # Update UserLogs with HTTP error status
                    user_log.value2 = "failure"
                    user_log.value3 = "0"
                    user_log.key4 = "error"
                    user_log.value4 = f"HTTP {response.status_code}: {response.text}"
                    user_log.save()
                    print(f"Updated UserLogs ID {user_log.id} with HTTP error status")

                    return {
                        "success": False,
                        "message": f"API call failed with status {response.status_code}: {response.text}",
                    }

            except requests.exceptions.RequestException as e:
                print(f"Request error: {e}")

                # Update UserLogs with request error status
                user_log.value2 = "failure"
                user_log.value3 = "0"
                user_log.key4 = "error"
                user_log.value4 = f"Request error: {e}"
                user_log.save()
                print(f"Updated UserLogs ID {user_log.id} with request error status")

                return {"success": False, "message": f"Request error: {e}"}

        except Exception as e:
            print(f"Error in process_and_submit_work_demand: {e}")
            import traceback

            traceback.print_exc()

            # Update UserLogs with internal error status
            try:
                user_log.value2 = "failure"
                user_log.value3 = "0"
                user_log.key4 = "error"
                user_log.value4 = f"Internal error: {e}"
                user_log.save()
                print(f"Updated UserLogs ID {user_log.id} with internal error status")
            except Exception as save_error:
                print(f"Failed to update UserLogs: {save_error}")

            return {"success": False, "message": f"Internal error: {e}"}

    def fetch_work_demand_status(self, bot_instance_id, data_dict):
        """
        Fetches work demand status for the current user from Community Engagement API.

        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Contains user_id, bot_id, and other session data

        Returns:
            str: "has_work_demands" if user has work demands, "no_work_demands" if none found, "failure" on error
        """
        print(
            f"Fetching work demand status for bot_instance_id: {bot_instance_id} and data_dict: {data_dict}"
        )
        try:
            import requests
            from django.conf import settings
            from bot_interface.models import BotUsers
            # import requests  # Placeholder for external API call

            # Get user information
            user_id = data_dict.get("user_id")
            bot_id = data_dict.get("bot_id", 1)

            if not user_id:
                print("No user_id found in data_dict")
                return "failure"

            # Get user's contact number
            try:
                bot_user = BotUsers.objects.get(pk=user_id)
                contact_number = bot_user.user.contact_number
            except BotUsers.DoesNotExist:
                print(f"BotUsers with id {user_id} not found")
                return "failure"

            # Get user's active community
            try:
                community_mapping = Community_user_mapping.objects.filter(
                    user=bot_user.user, is_last_accessed_community=True
                ).first()

                if not community_mapping:
                    print(f"No active community found for user {contact_number}")
                    return "failure"

                community_id = community_mapping.community.id
            except Exception as e:
                print(f"Error getting community for user {contact_number}: {e}")
                return "failure"

            # Call Community Engagement API
            api_url = f"{CE_API_URL}get_items_status/"
            params = {
                "number": contact_number,
                "bot_id": bot_instance_id,
                # 'community_id': str(community_id),
                "work_demand_only": "true",
            }

            print(
                f"Fetching work demand status for user {contact_number} in community {community_id} and bot_id {bot_instance_id}"
            )
            response = requests.get(api_url, params=params, timeout=30)
            print("response from GET request of get_items_status/ :", response)

            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    work_demands = result.get("data", [])
                    print(
                        f"Found {len(work_demands)} work demands for user {contact_number}"
                    )

                    # Store work demands in user session for persistence between states
                    try:
                        from bot_interface.models import UserSessions

                        session_data = {
                            "work_demands": work_demands,
                            "community_id": community_id,
                        }
                        UserSessions.objects.filter(user_id=user_id).update(
                            misc_data=session_data
                        )
                        print(
                            f"Stored {len(work_demands)} work demands in session for user {user_id}"
                        )
                    except Exception as session_error:
                        print(f"Error storing work demands in session: {session_error}")

                    if work_demands:
                        return "has_work_demands"
                    else:
                        return "no_work_demands"
                else:
                    print(
                        f"API returned error: {result.get('message', 'Unknown error')}"
                    )
                    return "failure"
            else:
                print(
                    f"API request failed with status {response.status_code}: {response.text}"
                )
                return "failure"

        except Exception as e:
            print(f"Error in fetch_work_demand_status: {e}")
            return "failure"

    def display_multiple_community_message(self, bot_instance_id, data_dict):
        """
        Display welcome message for users with multiple communities.
        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Dictionary containing user and session data.
        Returns:
            str: "success" or "failure"
        """
        print("in display_multiple_community_message")

        try:
            bot_instance = bot_interface.models.Bot.objects.get(id=bot_instance_id)
            user_id = data_dict.get("user_id")

            # Get user session
            user = bot_interface.models.UserSessions.objects.get(
                user=user_id, bot=bot_instance
            )

            # Get BotUsers object to access user_misc
            bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)
            current_communities = bot_user.user_misc.get(
                "community_membership", {}
            ).get("current_communities", [])

            if len(current_communities) > 0:
                # Get fresh community data with last accessed info
                success, api_response = (
                    bot_interface.utils.check_user_community_status_http(user.phone)
                )
                if success and api_response.get("success"):
                    community_data = api_response.get("data", {})
                    last_accessed_id = community_data.get("misc", {}).get(
                        "last_accessed_community_id"
                    )

                    # Find the last accessed community name
                    communities_list = community_data.get("data", [])
                    last_community_name = "Unknown Community"
                    for community in communities_list:
                        if community.get("community_id") == last_accessed_id:
                            last_community_name = community.get(
                                "name", "Unknown Community"
                            )
                            break
                else:
                    # Fallback to first community
                    last_community_name = current_communities[0].get(
                        "community_name", "Unknown Community"
                    )

                # Create welcome message
                welcome_text = (
                    f"🏠 आपने पिछली बार {last_community_name} समुदाय का उपयोग किया था।"
                )

                # Send text message
                response = bot_interface.api.send_text(
                    bot_instance_id=bot_instance_id,
                    contact_number=user.phone,
                    text=welcome_text,
                )

                print(f"Multiple community welcome message sent: {response}")

                if response and response.get("messages"):
                    return "success"
                else:
                    return "failure"
            else:
                print("No communities found for user")
                return "failure"

        except Exception as e:
            print(f"Error in display_multiple_community_message: {e}")
            return "failure"

    def display_single_community_message(self, bot_instance_id, data_dict):
        """
        Display welcome message for users with a single community.
        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Dictionary containing user and session data.
        Returns:
            str: "success" or "failure"
        """
        print("in display_single_community_message")

        try:
            bot_instance = bot_interface.models.Bot.objects.get(id=bot_instance_id)
            user_id = data_dict.get("user_id")

            # Get user session
            user = bot_interface.models.UserSessions.objects.get(
                user=user_id, bot=bot_instance
            )

            # Get BotUsers object to access user_misc
            bot_user = bot_interface.models.BotUsers.objects.get(id=user_id)

            current_communities = bot_user.user_misc.get(
                "community_membership", {}
            ).get("current_communities", [])

            # Single community expected
            if len(current_communities) != 1:
                print("User does not have exactly one community")
                return "failure"

            community_name = current_communities[0].get("community_name", "आपका समुदाय")

            # Try to refresh from API (optional but consistent)
            success, api_response = (
                bot_interface.utils.check_user_community_status_http(user.phone)
            )

            if success and api_response.get("success"):
                community_data = api_response.get("data", {})
                communities_list = community_data.get("data", [])

                # Prefer authoritative API name
                if communities_list:
                    community_name = communities_list[0].get("name", community_name)

            # Create welcome message
            welcome_text = f"🏠 आप {community_name} समुदाय से जुड़े हुए हैं।"

            # Send text message
            response = bot_interface.api.send_text(
                bot_instance_id=bot_instance_id,
                contact_number=user.phone,
                text=welcome_text,
            )

            print(f"Single community welcome message sent: {response}")

            if response and response.get("messages"):
                return "success"

            return "failure"

        except Exception as e:
            print(f"Error in display_single_community_message: {e}")
            return "failure"

    def display_work_demands_text(self, bot_instance_id, data_dict):
        """
        Displays work demands as WhatsApp text message with Hindi format and character limit handling.

        Args:
            bot_instance_id (int): The ID of the bot instance.
            data_dict (dict): Contains user_id, bot_id, and other session data

        Returns:
            str: "success" if message sent successfully, "failure" otherwise
        """
        try:
            from bot_interface.models import UserSessions, BotUsers

            # Get user information
            user_id = data_dict.get("user_id")
            bot_id = data_dict.get("bot_id", 1)

            if not user_id:
                print("No user_id found in data_dict")
                return "failure"

            # Retrieve work demands from user session
            try:
                user_session = UserSessions.objects.filter(user_id=user_id).first()
                if not user_session or not user_session.misc_data:
                    print(f"No session data found for user {user_id}")
                    return "failure"

                session_data = user_session.misc_data
                work_demands = session_data.get("work_demands", [])

                if not work_demands:
                    print(f"No work demands found in session for user {user_id}")
                    return "failure"

            except Exception as session_error:
                print(f"Error retrieving session data: {session_error}")
                return "failure"

            # Get user's contact number for WhatsApp
            try:
                bot_user = BotUsers.objects.get(pk=user_id)
                contact_number = bot_user.user.contact_number
            except BotUsers.DoesNotExist:
                print(f"BotUsers with id {user_id} not found")
                return "failure"

            # Send work demands with character limit handling
            try:
                success = self._send_work_demands_with_limit(
                    work_demands, contact_number, bot_instance_id
                )
                if success:
                    print(f"Asset demands text sent successfully to {contact_number}")
                    return "success"
                else:
                    print(f"Failed to send asset demands text to {contact_number}")
                    return "failure"
            except Exception as send_error:
                print(f"Error sending WhatsApp message: {send_error}")
                return "failure"

        except Exception as e:
            print(f"Error in display_work_demands_text: {e}")
            return "failure"

    def _send_work_demands_with_limit(
        self, work_demands, contact_number, bot_instance_id, max_length=4000
    ):
        """
        Send work demands with character limit handling, splitting into multiple messages if needed.

        Args:
            work_demands (list): List of work demand objects
            contact_number (str): User's contact number
            bot_instance_id (int): Bot instance ID
            max_length (int): Maximum characters per message

        Returns:
            bool: True if all messages sent successfully, False otherwise
        """
        try:
            # Create base header
            header = "📋 आपके संसाधन की मांग की स्थिति:\n\n"

            # Calculate approximate length per work demand entry
            sample_entry = "1. संसाधन मांग ID: 123\n   शीर्षक: Asset Demand Request\n   स्थिति: UNMODERATED\n\n"
            entry_length = len(sample_entry)

            # Calculate how many entries can fit in one message
            available_space = (
                max_length - len(header) - 50
            )  # 50 chars buffer for part indicator
            entries_per_message = max(1, available_space // entry_length)

            total_messages = (
                len(work_demands) + entries_per_message - 1
            ) // entries_per_message

            # Send messages
            for msg_num in range(total_messages):
                start_idx = msg_num * entries_per_message
                end_idx = min(start_idx + entries_per_message, len(work_demands))

                # Create message text
                if total_messages == 1:
                    text = header
                else:
                    if msg_num == 0:
                        text = header
                    else:
                        text = f"📋 आपके संसाधन की मांग की स्थिति (जारी):\n\n"

                # Add work demand entries
                for i in range(start_idx, end_idx):
                    demand = work_demands[i]
                    demand_id = demand.get("id", "N/A")
                    title = demand.get("title", "Asset Demand Request")
                    status = demand.get("status", "UNMODERATED")
                    transcription = demand.get("transcription", "")

                    text += f"{i + 1}. संसाधन मांग ID: {demand_id}\n"
                    text += f"   शीर्षक: {title}\n"
                    text += f"   स्थिति: {status}\n"

                    # Add transcription if available and not empty
                    if transcription and transcription.strip():
                        # Truncate long transcriptions
                        if len(transcription) > 50:
                            transcription = transcription[:50] + "..."
                        text += f"   विवरण: {transcription}\n"

                    text += "\n"

                # Add part indicator for multiple messages
                if total_messages > 1:
                    text += f"(भाग {msg_num + 1}/{total_messages})"

                # Send message
                response = bot_interface.api.send_text(
                    bot_instance_id=bot_instance_id,
                    contact_number=contact_number,
                    text=text,
                )

                if not response or not response.get("messages"):
                    print(f"Failed to send message part {msg_num + 1}/{total_messages}")
                    return False

                print(f"Sent message part {msg_num + 1}/{total_messages} successfully")

            return True

        except Exception as e:
            print(f"Error in _send_work_demands_with_limit: {e}")
            return False

    def send_weather_forecast_data_by_location(self, bot_instance_id, data_dict):
        """
        Send 5-day weather forecast data based on user's location.
        Creates a graph and sends as PNG to WhatsApp.
        Returns "success" or "failure"
        """
        logger.debug("=" * 70)
        logger.debug("send_weather_forecast_data_by_location (5-day forecast) CALLED")
        logger.debug(f"bot_instance_id: {bot_instance_id}")
        logger.debug(f"data_dict: {json.dumps(data_dict, indent=2, default=str)}")
        logger.debug("=" * 70)

        try:
            user_id = data_dict.get("user_id")
            bot_instance, user_session = _load_user_session(bot_instance_id, user_id)

            if not user_session:
                logger.error(f"User session not found for user_id: {user_id}")
                return "failure"

            logger.debug(f"User session phone: {user_session.phone}")

            # Get the getDataFrom configuration
            data = data_dict.get("data", {})
            get_data_from = data.get("getDataFrom", {})
            state_name = get_data_from.get("state")  # "SendLocationRequest"

            logger.debug(f"Looking for data from state: {state_name}")

            # Extract location from session using helper function
            latitude, longitude = _extract_lat_lon_from_session(
                user_session.current_session, state_name, "data"
            )

            if not latitude or not longitude:
                logger.error(f"Could not extract location from session")
                self.sendText(
                    bot_instance_id,
                    {
                        "user_id": user_id,
                        "text": [
                            {"hi": "❌ आपका स्थान नहीं मिल सका। कृपया पुनः प्रयास करें。"}
                        ],
                        "state": data_dict.get("state"),
                        "smj_id": data_dict.get("smj_id"),
                    },
                )
                return "failure"

            logger.debug(f"Final extracted location: lat={latitude}, lon={longitude}")

            # Call 5-day forecast API
            import requests
            import matplotlib.pyplot as plt
            import matplotlib

            matplotlib.use("Agg")  # Use non-interactive backend
            import tempfile
            import os
            from datetime import datetime

            # API endpoint for 5-day forecast
            weather_api_url = "http://127.0.0.1:8001/api/v1/weather/forecast/5-day/"
            params = {
                "lat": float(latitude),
                "lon": float(longitude),
            }

            logger.debug(f"Calling 5-day forecast API: {weather_api_url}")
            logger.debug(f"Params: {params}")

            try:
                response = requests.get(
                    weather_api_url,
                    params=params,
                    timeout=30,
                    headers={"Content-Type": "application/json"},
                )

                logger.debug(f"API Response Status Code: {response.status_code}")
                response.raise_for_status()
                weather_data = response.json()
                logger.debug("API response received successfully")

            except Exception as e:
                logger.error(f"API error: {str(e)}")
                self.sendText(
                    bot_instance_id,
                    {
                        "user_id": user_id,
                        "text": [
                            {"hi": "❌ मौसम डेटा प्राप्त नहीं हो सका। कृपया पुनः प्रयास करें。"}
                        ],
                        "state": data_dict.get("state"),
                        "smj_id": data_dict.get("smj_id"),
                    },
                )
                return "failure"

            # Extract data for plotting
            hourly_data = weather_data.get("hourly", {})
            times = hourly_data.get("time", [])
            temperatures = hourly_data.get("temperature_2m_c", [])
            precipitation = hourly_data.get("precipitation_mm_per_hour", [])

            if not times or not temperatures:
                logger.error("No hourly data found in API response")
                self.sendText(
                    bot_instance_id,
                    {
                        "user_id": user_id,
                        "text": [{"hi": "❌ मौसम डेटा उपलब्ध नहीं है。"}],
                        "state": data_dict.get("state"),
                        "smj_id": data_dict.get("smj_id"),
                    },
                )
                return "failure"

            # Process times for plotting
            times_dt = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times]

            # Create forecast summary by day
            daily_summary = {}
            for i, dt in enumerate(times_dt):
                date_key = dt.strftime("%Y-%m-%d")
                if date_key not in daily_summary:
                    daily_summary[date_key] = {"temps": [], "precip": [], "date": dt}
                daily_summary[date_key]["temps"].append(temperatures[i])
                if precipitation[i] is not None:
                    daily_summary[date_key]["precip"].append(precipitation[i])

            # Create figure with subplots
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), sharex=True)
            fig.suptitle(
                f"5-Day Weather Forecast\nLat: {latitude}, Lon: {longitude}",
                fontsize=16,
                fontweight="bold",
            )

            # Plot 1: Temperature
            ax1.plot(
                times_dt, temperatures, "r-", linewidth=2, label="Temperature (°C)"
            )
            ax1.fill_between(times_dt, temperatures, alpha=0.3, color="red")
            ax1.set_ylabel("Temperature (°C)", fontsize=12)
            ax1.legend(loc="upper right")
            ax1.grid(True, alpha=0.3)
            ax1.set_title("Temperature Forecast", fontsize=14)

            # Add markers for max/min points
            max_temp_idx = temperatures.index(max(temperatures))
            min_temp_idx = temperatures.index(min(temperatures))
            ax1.plot(
                times_dt[max_temp_idx],
                temperatures[max_temp_idx],
                "ro",
                markersize=10,
                label=f"Max: {temperatures[max_temp_idx]}°C",
            )
            ax1.plot(
                times_dt[min_temp_idx],
                temperatures[min_temp_idx],
                "bo",
                markersize=10,
                label=f"Min: {temperatures[min_temp_idx]}°C",
            )
            ax1.legend()

            # Plot 2: Precipitation
            precip_values = [p if p is not None else 0 for p in precipitation]
            ax2.bar(
                times_dt,
                precip_values,
                width=0.02,
                color="blue",
                alpha=0.7,
                label="Precipitation (mm/h)",
            )
            ax2.set_ylabel("Precipitation (mm/h)", fontsize=12)
            ax2.set_xlabel("Date/Time", fontsize=12)
            ax2.legend(loc="upper right")
            ax2.grid(True, alpha=0.3)
            ax2.set_title("Precipitation Forecast", fontsize=14)

            # Format x-axis
            import matplotlib.dates as mdates

            ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
            ax2.xaxis.set_major_locator(mdates.HourLocator(interval=12))
            plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha="right")

            plt.tight_layout()

            # Save plot to temporary file
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                temp_path = tmp_file.name
                plt.savefig(temp_path, dpi=100, bbox_inches="tight")
                plt.close()

            logger.debug(f"Graph saved to temporary file: {temp_path}")

            # Upload image to WhatsApp using the existing API
            try:
                # Upload media using your existing function
                from bot_interface.api import upload_media

                with open(temp_path, "rb") as file:
                    upload_response = upload_media(
                        bot_instance_id=bot_instance_id,
                        file_path=temp_path,
                        media_type="image/png",
                    )

                if upload_response and upload_response.get("id"):
                    media_id = upload_response.get("id")

                    # Send image using your existing send_text function with media
                    # Note: You may need to add a send_image method to your API
                    image_url = bot_interface.api.send_image(
                        bot_instance_id=bot_instance_id,
                        contact_number=user_session.phone,
                        media_id=media_id,
                        caption=f"🌤️ 5-दिन मौसम पूर्वानुमान - {latitude}, {longitude}",
                    )

                    logger.debug(f"Image sent with media ID: {media_id}")
                else:
                    raise Exception("Failed to upload media")

            except Exception as e:
                logger.warning(
                    f"Failed to upload/send image: {str(e)}. Sending text summary instead."
                )

                # Fallback to text summary
                summary_msg = "🌤️ *5-दिन मौसम पूर्वानुमान*\n\n"
                summary_msg += f"📍 स्थान: {latitude}, {longitude}\n\n"

                for date_key, data in sorted(daily_summary.items()):
                    avg_temp = sum(data["temps"]) / len(data["temps"])
                    max_temp = max(data["temps"])
                    min_temp = min(data["temps"])
                    total_precip = sum(data["precip"]) if data["precip"] else 0

                    date_formatted = data["date"].strftime("%d %b %Y")
                    summary_msg += f"📅 *{date_formatted}*\n"
                    summary_msg += f"   🌡️ तापमान: {avg_temp:.1f}°C (अधिकतम: {max_temp:.1f}°C, न्यूनतम: {min_temp:.1f}°C)\n"
                    summary_msg += f"   ☔ वर्षा: {total_precip:.1f} mm\n\n"

                self.sendText(
                    bot_instance_id,
                    {
                        "user_id": user_id,
                        "text": [{"hi": summary_msg}],
                        "state": data_dict.get("state"),
                        "smj_id": data_dict.get("smj_id"),
                    },
                )

            # Clean up temporary file
            try:
                os.unlink(temp_path)
            except:
                pass

            # Store forecast data in session
            if (
                not hasattr(user_session, "current_session")
                or user_session.current_session is None
            ):
                user_session.current_session = []

            user_session.current_session.append(
                {
                    "weather_5day_forecast": {
                        "data": weather_data,
                        "displayed": True,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "location": {"lat": latitude, "lon": longitude},
                    }
                }
            )
            user_session.save()
            logger.debug("5-day forecast data stored in session")

            return "success"

        except Exception as e:
            logger.exception(
                f"FATAL Error in send_weather_forecast_data_by_location: {str(e)}"
            )
            import traceback

            logger.error(traceback.format_exc())
            try:
                if "user_id" in data_dict and "user_session" in locals():
                    self.sendText(
                        bot_instance_id,
                        {
                            "user_id": user_id,
                            "text": [{"hi": "❌ कुछ गलती हुई। कृपया पुनः प्रयास करें。"}],
                            "state": data_dict.get("state"),
                            "smj_id": data_dict.get("smj_id"),
                        },
                    )
            except:
                pass
            return "failure"

    def send_crop_advisory_data_by_location(self, bot_instance_id, data_dict):
        """
        Send crop advisory data based on user's location, crop type, and advisory type.
        """
        logger.debug("send_crop_advisory_data_by_location called")

        try:
            user_id = data_dict.get("user_id")
            bot_instance, user_session = _load_user_session(bot_instance_id, user_id)

            data = data_dict.get("data", {})
            get_data_from = data.get("getDataFrom", [])

            # Extract data from different states
            location_state = None
            advisory_type_state = None
            crop_state = None

            for item in get_data_from:
                if item.get("state") == "SendLocationRequest":
                    location_state = item.get("state")
                elif item.get("state") == "SelectAdvisoryType":
                    advisory_type_state = item.get("state")
                elif item.get("state") == "SelectCrop":
                    crop_state = item.get("state")

            user_session.current_state = data_dict.get("state")

            # Extract latitude and longitude
            latitude, longitude = _extract_lat_lon_from_session(
                user_session.current_session, location_state, "data"
            )

            if not latitude or not longitude:
                logger.error("Location data not found in user session")
                bot_interface.api.send_text_message(
                    bot_instance_id=bot_instance_id,
                    contact_number=user_session.phone,
                    text="Could not find your location. Please share your location again.",
                )
                return "failure"

            # Extract advisory type and crop
            advisory_type = None
            selected_crop = None

            for session_item in user_session.current_session:
                if advisory_type_state in session_item:
                    advisory_data = session_item[advisory_type_state]
                    advisory_type = advisory_data.get("misc") or advisory_data.get(
                        "data"
                    )

                if crop_state in session_item:
                    crop_data = session_item[crop_state]
                    selected_crop = crop_data.get("misc") or crop_data.get("data")

            if not advisory_type or not selected_crop:
                logger.error("Missing advisory type or crop selection")
                bot_interface.api.send_text_message(
                    bot_instance_id=bot_instance_id,
                    contact_number=user_session.phone,
                    text="Missing advisory information. Please start over.",
                )
                return "failure"

            # Build API URL for crop advisory
            crop_api_url = f"http://127.0.0.1:8001/api/v1/crop/advisory/"

            import requests
            from datetime import datetime

            params = {
                "lat": float(latitude),
                "lon": float(longitude),
                "crop": selected_crop,
                "advisory_type": advisory_type,
                "datetime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }

            logger.debug(
                f"Fetching crop advisory from API: {crop_api_url} with params: {params}"
            )

            try:
                response = requests.get(crop_api_url, params=params, timeout=10)
                response.raise_for_status()
                advisory_data = response.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching crop advisory: {str(e)}")
                bot_interface.api.send_text_message(
                    bot_instance_id=bot_instance_id,
                    contact_number=user_session.phone,
                    text="Sorry, unable to fetch crop advisory at the moment. Please try again later.",
                )
                return "failure"

            # Format and send advisory message
            advisory_message = f"🌱 *Crop Advisory*\n\n"
            advisory_message += f"📍 *Location:* Lat: {latitude}, Lon: {longitude}\n"
            advisory_message += f"🌾 *Crop:* {selected_crop}\n"
            advisory_message += f"📋 *Advisory Type:* {advisory_type}\n\n"

            # Add advisory content based on response structure
            if "recommendations" in advisory_data:
                advisory_message += "*Recommendations:*\n"
                for rec in advisory_data["recommendations"]:
                    advisory_message += f"• {rec}\n"

            if "weather_impact" in advisory_data:
                advisory_message += (
                    f"\n*Weather Impact:*\n{advisory_data['weather_impact']}\n"
                )

            if "additional_notes" in advisory_data:
                advisory_message += (
                    f"\n*Additional Notes:*\n{advisory_data['additional_notes']}"
                )

            # Send the advisory
            bot_interface.api.send_text_message(
                bot_instance_id=bot_instance_id,
                contact_number=user_session.phone,
                text=advisory_message,
            )

            # Store advisory data in session
            user_session.current_session.append({"crop_advisory_data": advisory_data})

            user_session.save()

            return "success"

        except Exception as e:
            print(f"Error in send_crop_advisory_data_by_location: {str(e)}")
            return "failure"

    # Helpers matching reference project patterns
    def _load_user_session(self, bot_id, user_id):
        from bot_interface.models import UserSessions
        return UserSessions.objects.get(user_id=user_id, bot_id=bot_id)



    def _generate_weather_card_image(self, day_data):
        """Generate individual weather card image (kept for fallback/potential use)"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import io
        import math
        import random

        # ... (implementation remains same but we'll focus on the combined one)
        return self._generate_combined_weather_report_image([day_data])

    def _generate_combined_weather_report_image(self, forecast_list, current_temp=None, current_precip=None, location_name="Your Location"):
        """Generate a SINGLE advanced UI card for the full 5-day forecast."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import io
        import math
        import random

        # ... (palette logic) ...
        bg_gradient = ['#1A237E', '#311B92']  # Deep Midnight Blue
        card_bg = 'white'
        row_bg_alpha = 0.12
        text_white = '#FFFFFF'
        text_muted = '#B0BEC5'

        fig, ax = plt.subplots(figsize=(6, 10))
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis('off')

        # 🌆 Background: Premium Gradient
        ax.add_patch(mpatches.Rectangle((0, 0), 1, 1, color=bg_gradient[0], transform=ax.transAxes))
        for _ in range(5):
            rx, ry = random.uniform(0.1, 0.9), random.uniform(0.1, 0.9)
            ax.add_patch(mpatches.Circle((rx, ry), 0.4, color=bg_gradient[1], alpha=0.3, transform=ax.transAxes))

        # 🏷️ HEADER
        ax.text(0.5, 0.94, "Weather Forecast", fontsize=16, color=text_white, ha='center', weight='bold', alpha=0.9)
        
        # Display the dynamic location name (truncated if too long)
        display_location = str(location_name)[:35] + "..." if len(str(location_name)) > 35 else str(location_name)
        ax.text(0.5, 0.91, display_location, fontsize=10, color=text_muted, ha='center')
        
        # Current Highlight (Real-time data)
        if forecast_list:
            top = forecast_list[0]
            display_temp = current_temp if current_temp is not None else top['max_temp']
            display_precip = current_precip if current_precip is not None else top['precip']
            
            # Big Current Temp
            ax.text(0.5, 0.82, f"{display_temp}°C", fontsize=52, color=text_white, ha='center', weight='bold')
            ax.text(0.5, 0.77, f"NOW: {top['condition'].upper()} | ☔ {display_precip}mm", fontsize=14, color=text_white, ha='center', alpha=0.8)
            
        start_y = 0.68
        step_y = 0.12
        
        for i, day in enumerate(forecast_list):
            y_pos = start_y - (i * step_y)
            
            # Row Background (Glassmorphism row)
            rect = mpatches.FancyBboxPatch(
                (0.05, y_pos - 0.051), 0.9, 0.102,
                boxstyle="round,pad=0,rounding_size=0.03",
                linewidth=0.5, edgecolor='white', facecolor='white', alpha=row_bg_alpha,
                transform=ax.transAxes
            )
            ax.add_patch(rect)
            
            # 1. Date Column (Left)
            date_label = "Today" if i == 0 else day['date'].split(',')[0]
            ax.text(0.08, y_pos, date_label, fontsize=13, color=text_white, ha='left', va='center', weight='semibold')
            
            # 2. Icon Column (Manual drawing for reliability)
            cond = day['condition'].lower()
            ix, iy = 0.35, y_pos
            if "sun" in cond or "clear" in cond:
                ax.add_patch(mpatches.Circle((ix, iy), 0.015, color='#FFD54F', transform=ax.transAxes))
                for j in range(8):
                    ang = math.radians(j * 45)
                    ax.plot([ix + 0.02*math.cos(ang), ix + 0.03*math.cos(ang)],
                            [iy + 0.02*math.sin(ang), iy + 0.03*math.sin(ang)], color='#FFD54F', lw=1.5)
            elif "rain" in cond:
                ax.add_patch(mpatches.Ellipse((ix, iy+0.005), 0.04, 0.025, color='white', transform=ax.transAxes))
                for ox in [-0.01, 0, 0.01]:
                    ax.plot([ix+ox, ix+ox-0.003], [iy-0.01, iy-0.025], color='#81D4FA', lw=1.5)
            else:
                ax.add_patch(mpatches.Ellipse((ix-0.01, iy), 0.03, 0.02, color='#B0BEC5', transform=ax.transAxes))
                ax.add_patch(mpatches.Ellipse((ix+0.01, iy), 0.03, 0.02, color='#B0BEC5', transform=ax.transAxes))
                ax.add_patch(mpatches.Circle((ix, iy+0.01), 0.02, color='#B0BEC5', transform=ax.transAxes))
            
            # 3. Temperature Column (Max / Min)
            max_t = f"{round(day['max_temp'])}°"
            min_t = f"{round(day['min_temp'])}°"
            ax.text(0.55, y_pos, max_t, fontsize=15, color=text_white, ha='right', va='center', weight='bold')
            ax.text(0.60, y_pos, "/", fontsize=12, color=text_muted, ha='center', va='center')
            ax.text(0.65, y_pos, min_t, fontsize=13, color=text_muted, ha='left', va='center')
            
            # 4. Precipitation Column (Rain)
            precip = f"{day['precip']} mm"
            ax.text(0.91, y_pos, precip, fontsize=11, color='#81D4FA', ha='right', va='center', weight='semibold')

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=140, bbox_inches='tight', pad_inches=0.1)
        buf.seek(0)
        plt.close(fig)
        return buf


    def handle_weather_forecast(self, bot_instance_id, data_dict):
        """
        Handle weather forecast with interactive carousel
        """
        import os
        import random
        from datetime import datetime
        from collections import defaultdict
        from bot_interface.api import send_image, send_button_msg

        logger.debug("handle_weather_forecast (Carousel version) called")

        user_id = data_dict.get("user_id")
        try:
            user_session = self._load_user_session(bot_instance_id, user_id)
        except Exception as e:
            logger.error(f"handle_weather_forecast: could not load session – {e}")
            return "failure"

        phone = user_session.phone

        # ── 1. Extract lat/lon ───────────────────────────────────────────────
        loc = _extract_location_data(user_session, data_dict)
        if not loc:
            for item in (user_session.current_session or []):
                if isinstance(item, dict):
                    loc_entry = item.get("request_location_location") or item.get("request_location")
                    if isinstance(loc_entry, dict):
                        misc = loc_entry.get("misc", {})
                        raw = loc_entry.get("data", "")
                        if misc.get("latitude") and misc.get("longitude"):
                            loc = {
                                "latitude": str(misc["latitude"]), 
                                "longitude": str(misc["longitude"]),
                                "name": misc.get("name") or misc.get("address")
                            }
                        elif isinstance(raw, str) and "," in raw:
                            parts = raw.split(",", 1)
                            loc = {"latitude": parts[0].strip(), "longitude": parts[1].strip()}
                        if loc:
                            break

        if not loc:
            bot_interface.api.send_text(bot_instance_id, phone, 
                                    "❌ आपका स्थान नहीं मिल सका। कृपया पुनः प्रयास करें।")
            return "failure"

        lat = loc.get("latitude")
        lon = loc.get("longitude")
        location_name = loc.get("name") or "Your Location"
        
        # Send typing indicator
        bot_interface.api.send_action(bot_instance_id, phone, "typing")

        # ── 2. Call weather API ──────────────────────────────────────────────
        from bot_interface.api_stubs import get_weather_forecast
        weather_data = get_weather_forecast(lat, lon)

        hourly = weather_data.get("hourly", {})
        times_raw = hourly.get("time", [])
        temps = hourly.get("temperature_2m_c", [])
        precips = hourly.get("precipitation_mm_per_hour", [])

        if not times_raw:
            bot_interface.api.send_text(bot_instance_id, phone, 
                                    "❌ मौसम डेटा उपलब्ध नहीं है। कृपया बाद में प्रयास करें।")
            return "failure"

        # ── 3. Aggregate into Daily Data ──────────────────────────────────────
        daily_map = defaultdict(lambda: {"temps": [], "precips": []})
        for t, temp, precip in zip(times_raw, temps, precips):
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                key = dt.strftime("%Y-%m-%d")
                daily_map[key]["temps"].append(temp if temp is not None else 25.0)
                daily_map[key]["precips"].append(precip if precip is not None else 0.0)
                daily_map[key]["display_date"] = dt.strftime("%a, %b %d")
            except Exception as e:
                logger.error(f"Date parsing error: {e}")
                continue

        # Prepare forecast list
        forecast_list = []
        for key in sorted(daily_map.keys())[:5]:
            d = daily_map[key]
            avg_precip = sum(d["precips"])
            is_rainy = avg_precip > 0.5
            
            # ── Improved Weather Condition Logic ──
            if avg_precip > 0.1:
                condition = "Rainy"
                condition_hindi = "बारिश"
            elif round(max(d["temps"])) > 32:
                condition = "Sunny"
                condition_hindi = "धूप"
            else:
                condition = "Cloudy"
                condition_hindi = "बादल"
            
            forecast_list.append({
                "date": d["display_date"],
                "max_temp": round(max(d["temps"]), 1),
                "min_temp": round(min(d["temps"]), 1),
                "precip": round(avg_precip, 1),
                "humidity": random.randint(45, 85),
                "condition": condition,
                "condition_hindi": condition_hindi
            })

        # ── 4. Generate Cards and Prepare Carousel ────────────────────────────
        cards = []
        try:
            for idx, day in enumerate(forecast_list):
                # Generate image
                img_buf = self._generate_weather_card_image(day)
                
                # Save locally for reference/debugging
                filename = f"weather_{idx}_{day['date'].replace(',', '').replace(' ', '_')}.png"
                local_path = os.path.join(bot_interface.api.WHATSAPP_MEDIA_PATH, filename)
                try:
                    # Create directory if it doesn't exist
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(img_buf.getvalue())
                    logger.debug(f"Saved weather card locally: {local_path}")
                except Exception as e:
                    logger.error(f"Error saving local copy: {e}")

                # Upload image to WhatsApp from local path
                upload_resp = bot_interface.api.upload_media(
                    bot_instance_id=bot_instance_id,
                    file_path=local_path,
                    media_type="image/png"
                )
                
                # Cloud API returns media ID in 'id' field
                media_id = None
                if upload_resp:
                    media_id = upload_resp.get("id") or upload_resp.get("media_id")
                
                if media_id:
                    # Prepare card for carousel
                    card = {
                        "media_id": media_id,
                        "media_type": "image",
                        "body_text": f"📅 {day['date']}\n🌡️ {day['max_temp']}°C / {day['min_temp']}°C\n☔ {day['precip']} mm rain",
                        "buttons": [
                            {
                                "type": "reply",
                                "id": f"hourly_{idx}",
                                "title": "Hourly Details"
                            },
                            {
                                "type": "reply", 
                                "id": f"share_{idx}",
                                "title": "Share"
                            }
                        ]
                    }
                    cards.append(card)
                else:
                    logger.error(f"Failed to upload card for {day['date']}")
                    
        except Exception as e:
            logger.error(f"Error generating carousel cards: {e}")
            import traceback
            traceback.print_exc()

        # ── 5. Send High-Performance Single Card Report ────────────────────
        from bot_interface.api import send_image

        # Identify "Current" conditions (closest hour to now) ──
        now = datetime.now(timezone.UTC)
        current_temp, current_precip = forecast_list[0]['max_temp'], forecast_list[0]['precip'] # Default to first day's max temp and precip
        try:
            # Find the hour closest to current time (normalizing to naive for comparison)
            diffs = [(abs((datetime.fromisoformat(t.replace("Z", "+00:00")).replace(tzinfo=None) - now.replace(tzinfo=None)).total_seconds()), t_val, p_val) 
                     for t, t_val, p_val in zip(times_raw, temps, precips)]
            if diffs:
                best = min(diffs, key=lambda x: x[0])
                current_temp = round(best[1], 1)
                current_precip = round(best[2], 1)
        except Exception as e:
            logger.error(f"Error finding current conditions: {e}")

        # 4. Media Caching: Avoid re-generating and re-uploading identical cards within 1 hour
        from django.core.cache import cache
        # Safe cache key: replace spaces (Memcached compatibility)
        safe_location = location_name.replace(" ", "_")
        media_cache_key = f"weather_media_{round(float(lat), 2)}_{round(float(lon), 2)}_{safe_location}"
        cached_media_id = cache.get(media_cache_key)
        
        if cached_media_id:
            logger.info(f"Re-using cached weather media_id: {cached_media_id}")
            send_image(
                bot_instance_id,
                phone,
                cached_media_id,
                caption=f"🌤️ *5-Day Weather Forecast*\nYour complete weather summary for {location_name}."
            )
            return "success"

        # 6. Generate and Upload Single Card Report
        if forecast_list:
            logger.debug(f"Generating single-card report for {location_name}")
            report_buf = self._generate_combined_weather_report_image(
                forecast_list, current_temp, current_precip, location_name=location_name
            )
            
            # Use unique filename via current timestamp (requires 'import time' at top)
            report_filename = f"weather_{phone}_{int(time.time())}.png"
            report_path = os.path.join(bot_interface.api.WHATSAPP_MEDIA_PATH, report_filename)
            with open(report_path, "wb") as f:
                f.write(report_buf.getbuffer())

            upload_resp = bot_interface.api.upload_media(
                bot_instance_id=bot_instance_id,
                file_path=report_path,
                media_type="image/png"
            )

            if upload_resp and upload_resp.get("id"):
                media_id = upload_resp["id"]
                # Save to cache for 1 hour
                cache.set(media_cache_key, media_id, timeout=3600)
                
                send_image(
                    bot_instance_id,
                    phone,
                    media_id,
                    caption=f"🌤️ *5-Day Weather Forecast*\nYour complete weather summary for {forecast_list[0]['date']} to {forecast_list[-1]['date']}."
                )
                
                # Cleanup temporary file
                try: os.unlink(report_path)
                except Exception as e: 
                    logger.warning(f"Could not delete temp weather file: {e}")
                
                return "success"

            return "failure"


    def handle_crop_advisory(self, bot_instance_id, data_dict):
        """Handle crop advisory logic using API stubs."""
        from bot_interface.api_stubs import get_crop_advisory
        from bot_interface.api import send_text
        
        user_session = self._load_user_session(bot_instance_id, data_dict.get("user_id"))
        loc_data = _extract_location_data(user_session, data_dict)
        if not loc_data:
            send_text(bot_instance_id, user_session.phone, "❌ Location data not found. Please try sharing your location again.")
            return "failure"
        lat, lon = loc_data.get("latitude"), loc_data.get("longitude")
        
        # In Crop_Flow, request_crop_data state precedes request_location
        # The user input for crop name is in event_data['data'] if this transition comes from there
        crop_name = data_dict.get("event_data", {}).get("data", "Wheat")
        
        advisory = get_crop_advisory(crop_name, "2026-03-20", lat, lon)
        send_text(bot_instance_id, user_session.phone, f"🌱 *Crop Advisory*\n\n{advisory}")
        return "success"

    def check_villages_by_location(self, bot_instance_id, data_dict):
        """Check for villages based on location and update session."""
        from bot_interface.api_stubs import get_villages_by_location, check_user_village
        from bot_interface.api import send_text
        print("data_dict-- for check_villages_by_location", data_dict)
        
        user_session = self._load_user_session(bot_instance_id, data_dict.get("user_id"))
        phone = user_session.phone
        
        # 1. Check if user is already a registered community member
        reg_info = check_user_village(phone)
        print("reg_info-- for check_villages_by_location", reg_info)
        
        # Save community_id and community_name in BotUser user_misc field
        if reg_info and isinstance(reg_info, dict):
            bot_user = user_session.user
            if not isinstance(bot_user.user_misc, dict):
                bot_user.user_misc = {}
            bot_user.user_misc["community_id"] = reg_info.get("id")
            bot_user.user_misc["village_name"] = reg_info.get("name")
            bot_user.user_misc["plan_id"] = reg_info.get("plan_id")
            bot_user.user_misc["plan_name"] = reg_info.get("plan_name")
            bot_user.save()


        state_name = data_dict.get("data", {}).get("getDataFrom", {}).get("state") or "request_location"
        lat, lon = _extract_lat_lon_from_session(
            user_session.current_session, state_name, "data"
        )
        print("lat for check_villages_by_location", lat)
        print("lon for check_villages_by_location", lon)

        if not lat or not lon:
            # Fallback to general extraction
            loc_data = _extract_location_data(user_session, data_dict)
            if loc_data:
                lat, lon = loc_data.get("latitude"), loc_data.get("longitude")

        if not lat or not lon:
            send_text(bot_instance_id, phone, "❌ Location data not found. Please try sharing your location again.")
            return "failure"
            
        # 2. Fetch nearby communities
        villages = get_villages_by_location(lat, lon)
        print("villages-- for check_villages_by_location", villages)
        
        # 3. Access Control and Decision Logic
        if not reg_info:
            # User is not a registered member - deny access as requested
            return "no_communities"
            
        if villages:
            # User IS a member - but we found villages for them to select/confirm (Old Flow)
            user_session.misc_data = user_session.misc_data or {}
            user_session.misc_data["available_villages"] = villages
            user_session.save()
            return "new_user"
            
        # User IS a member and no nearby villages to select - proceed with primary community
        return "registered"

    def display_village_list(self, bot_instance_id, data_dict):
        """Display the list of villages as a WhatsApp menu."""
        from bot_interface.api import send_list_msg
        
        user_session = self._load_user_session(bot_instance_id, data_dict.get("user_id"))
        user_lang = user_session.user_config.get("language") or "hi"
        villages = (user_session.misc_data or {}).get("available_villages", [])
        
        menu_items = [{"label": v["name"], "value": str(v["id"])} for v in villages]
        
        caption = "🏘️ *Please select your community:*" if user_lang == "en" else "🏘️ *कृपया अपना गांव/समुदाय चुनें:*"
        button_label = "Select" if user_lang == "en" else "चुनें"
        
        send_list_msg(bot_instance_id, user_session.phone, caption, menu_items, button_label=button_label)
        
        # Fix: Save current state before waiting for user interaction!
        user_session.current_state = data_dict.get("state")
        user_session.save()
        
        return "interactive"

    def handle_join_village(self, bot_instance_id, data_dict):
        """Handle joining a selected village."""
        from bot_interface.api_stubs import join_village
        from bot_interface.api import send_button_msg
        from bot_interface.models import BotUsers
        print("inside handle join village 1")
        
        user_session = self._load_user_session(bot_instance_id, data_dict.get("user_id"))
        village_id = data_dict.get("event_data", {}).get("misc") or data_dict.get("event_data", {}).get("data")
        print("inside handle join village 2")
        
        if village_id:
            join_village(user_session.phone, village_id)
            user_session.misc_data = user_session.misc_data or {}
            user_session.misc_data["village_id"] = village_id
            print("inside handle join village 3")
            
            # Find the selected village from available_villages
            available_villages = user_session.misc_data.get("available_villages", [])
            selected_village_data = next((v for v in available_villages if str(v["id"]) == str(village_id)), None)
            print("inside handle join village 4")
            
            # Store data in BotUsers user_misc
            bot_user = BotUsers.objects.get(id=data_dict.get("user_id"))
            bot_user.user_misc = bot_user.user_misc or {}
            if selected_village_data:
                bot_user.user_misc["selected_plan_data"] = selected_village_data
                village_name = selected_village_data.get("name", village_id)
            else:
                village_name = village_id
            bot_user.save()
            user_session.save()
            print("inside handle join village 5")
            
            user_lang = user_session.user_config.get("language") or "hi"
            # Provide 2 options menu
            menu_list = [
                {
                    "label": "Continue" if user_lang == "en" else "जारी रखें", 
                    "value": "continue"
                },
                {
                    "label": "Change Village" if user_lang == "en" else "गांव बदलें", 
                    "value": "change_village"
                }
            ]
            welcome_text = f"Welcome to {village_name} village" if user_lang == "en" else f"{village_name} गांव में आपका स्वागत है"
            send_button_msg(bot_instance_id, user_session.phone, welcome_text, menu_list)
            print("inside handle join village 6")
            # Fix: Save current state before waiting for user interaction!
            user_session.current_state = data_dict.get("state")
            user_session.save()
            print("inside handle join village 7")
            
            return "success"
        return "failure"

    def handle_village_confirmation(self, bot_instance_id, data_dict):
        """Handle user choice between continue and change village."""
        choice = data_dict.get("event_data", {}).get("misc") or data_dict.get("event_data", {}).get("data")
        
        if choice in ["continue", "change_village"]:
            return choice
            
        return "failure"

    def handle_create_asset_demand(self, bot_instance_id, data_dict):
        """Handle asset demand creation."""
        from bot_interface.api_stubs import create_asset_demand
        from bot_interface.api import send_text
        
        user_session = self._load_user_session(bot_instance_id, data_dict.get("user_id"))
        village_id = (user_session.misc_data or {}).get("village_id")
        
        # Log media ID if present
        media_id = data_dict.get("event_data", {}).get("media_id", "N/A")
        print(f"DEBUG: Creating demand for village {village_id} with Media ID: {media_id}")
        
        res = create_asset_demand(user_session.user_id, village_id)
        send_text(bot_instance_id, user_session.phone, f"📋 {res['message']}")
        return "success"

    def handle_create_story(self, bot_instance_id, data_dict):
        """Handle story creation."""
        from bot_interface.api_stubs import create_story
        from bot_interface.api import send_text
        
        user_session = self._load_user_session(bot_instance_id, data_dict.get("user_id"))
        village_id = (user_session.misc_data or {}).get("village_id")
        
        res = create_story(user_session.user_id, village_id)
        send_text(bot_instance_id, user_session.phone, f"📖 {res['message']}")
        return "success"

    def handle_view_demands(self, bot_instance_id, data_dict):
        """Handle viewing village asset demands using API stubs."""
        from bot_interface.api_stubs import fetch_asset_demands
        from bot_interface.api import send_text
        
        user_session = self._load_user_session(bot_instance_id, data_dict.get("user_id"))
        village_id = (user_session.misc_data or {}).get("village_id")
        demands = fetch_asset_demands(village_id)
        
        if not demands:
            msg = "❌ You have no asset Demand available. Back to Menu."
        else:
            msg = "📊 *Active Asset Demands:*\n\n"
            for d in demands:
                status_emoji = "✅" if d['status'] == "Approved" else "⏳"
                msg += f"{status_emoji} {d['asset']}: {d['status']}\n"
        
        send_text(bot_instance_id, user_session.phone, msg)
        return "success"

    def handle_view_stories(self, bot_instance_id, data_dict):
        """Handle viewing village stories using API stubs."""
        from bot_interface.api_stubs import fetch_stories
        from bot_interface.api import send_text
        
        user_session = self._load_user_session(bot_instance_id, data_dict.get("user_id"))
        village_id = (user_session.misc_data or {}).get("village_id")
        stories = fetch_stories(village_id)
        
        if not stories:
            msg = "❌ No stories available. Back to Menu."
        else:
            msg = "📜 *Community Success Stories:*\n\n"
            for s in stories:
                msg += f"⭐ *{s['title']}*\n_{s['content']}_\n\n"
        
        send_text(bot_instance_id, user_session.phone, msg)
        return "success"

    def set_language(self, bot_instance_id, data_dict):
        """Set user's language preference and update session."""
        try:
            user_id = data_dict.get("user_id")
            user_session = self._load_user_session(bot_instance_id, user_id)
            
            # The event_data contains the choice (hi or en) from button/menu
            choice = data_dict.get("event_data", {}).get("misc") or data_dict.get("event_data", {}).get("data")
            
            print(f"DEBUG: set_language called with choice: {choice}")
            
            if choice not in ["hi", "en"]:
                # Check if it's in data directly
                if data_dict.get("data") in ["hi", "en"]:
                    choice = data_dict.get("data")
                else:
                    choice = "hi"
            
            # Update user_config
            if not isinstance(user_session.user_config, dict):
                user_session.user_config = {}
            user_session.user_config["language"] = choice
            user_session.save()
            
            print(f"Language set to {choice} for user {user_id}")
            return "success"
        except Exception as e:
            print(f"Error in set_language: {e}")
            return "failure"


    def jump_to_smj(self, bot_instance_id, data_dict):
        """Handle jumping to a target SMJ by name."""
        from bot_interface.models import SMJ
        import json
        
        target_smj_name = data_dict.get("data", {}).get("target_smj")
        print(f"DEBUG: jump_to_smj called with target: {target_smj_name}")
        
        if not target_smj_name:
            return "failure"
            
        try:
            smj_instance = SMJ.objects.get(name=target_smj_name)
            smj_states = smj_instance.smj_json
            if isinstance(smj_states, str):
                smj_states = json.loads(smj_states)
                
            data_dict["_smj_jump"] = {
                "smj_id": smj_instance.id,
                "smj_name": smj_instance.name,
                "states": smj_states,
                "init_state": smj_states[0]['name']
            }
            return "success"
        except Exception as e:
            print(f"ERROR in jump_to_smj: {e}")
            return "failure"

