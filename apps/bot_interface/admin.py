from django.contrib import admin
from .models import Bot, SMJ, BotUsers, UserSessions, UserLogs, UserArchive

@admin.register(Bot)
class BotAdmin(admin.ModelAdmin):
    list_display = ('bot_name', 'bot_number', 'app_type', 'smj', 'is_active')

@admin.register(SMJ)
class SMJAdmin(admin.ModelAdmin):
    list_display = ('name', 'desc', 'creation_time', 'last_updated_time')

@admin.register(BotUsers)
class BotUsersAdmin(admin.ModelAdmin):
    list_display = ('user', 'bot', 'first_interaction_at', 'last_updated_at')

@admin.register(UserSessions)
class UserSessionsAdmin(admin.ModelAdmin):
    list_display = ('user', 'bot', 'current_state', 'phone', 'started_at')

@admin.register(UserLogs)
class UserLogsAdmin(admin.ModelAdmin):
    list_display = ('user', 'bot', 'key1', 'value1', 'created_at')

@admin.register(UserArchive)
class UserArchiveAdmin(admin.ModelAdmin):
    list_display = ('user', 'bot', 'archived_at')
