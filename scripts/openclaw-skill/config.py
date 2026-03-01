"""Configuration loader for Yahoo Fantasy automation"""

import os
import yaml


DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

VALID_AUTONOMY_LEVELS = ("auto", "suggest", "alert", "off")

DEFAULT_ACTION = {
    "autonomy": "off",
    "schedule": "",
    "check_interval": 3600,
    "description": "",
}

DEFAULT_NOTIFICATIONS = {
    "enabled": False,
    "channel": "stdout",
    "min_priority": "medium",
}

DEFAULT_LEAGUE = {
    "api_url": "http://localhost:8766",
    "team_id": "",
}


class AutomationConfig:
    """Loads and manages automation configuration from YAML + env overrides."""

    def __init__(self, config_path=None):
        self._path = config_path or os.environ.get(
            "YF_AUTOMATION_CONFIG", DEFAULT_CONFIG_PATH
        )
        self._data = {}
        self.load()

    def load(self):
        """Load YAML config and apply environment variable overrides.

        Environment overrides use the pattern:
            YF_ACTION_<ACTION_NAME>_AUTONOMY=auto
            YF_NOTIFICATIONS_ENABLED=true
            YF_LEAGUE_API_URL=http://...
        """
        try:
            with open(self._path, "r") as fh:
                self._data = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            print("Config not found at " + str(self._path) + ", using defaults")
            self._data = {}
        except Exception as e:
            print("Error loading config: " + str(e) + ", using defaults")
            self._data = {}

        # Apply environment variable overrides for actions
        actions = self._data.get("actions", {})
        for action_name in actions:
            env_key = "YF_ACTION_" + action_name.upper() + "_AUTONOMY"
            env_val = os.environ.get(env_key, "")
            if env_val:
                level = env_val.lower().strip()
                if level in VALID_AUTONOMY_LEVELS:
                    actions[action_name]["autonomy"] = level
                else:
                    print(
                        "Ignoring invalid autonomy level '"
                        + env_val
                        + "' from "
                        + env_key
                    )

        # Apply environment overrides for notifications
        notif_enabled = os.environ.get("YF_NOTIFICATIONS_ENABLED", "")
        if notif_enabled:
            notif = self._data.get("notifications", {})
            if notif_enabled.lower() in ("true", "1", "yes"):
                notif["enabled"] = True
            elif notif_enabled.lower() in ("false", "0", "no"):
                notif["enabled"] = False
            self._data["notifications"] = notif

        notif_channel = os.environ.get("YF_NOTIFICATIONS_CHANNEL", "")
        if notif_channel:
            notif = self._data.get("notifications", {})
            notif["channel"] = notif_channel
            self._data["notifications"] = notif

        # Apply environment override for API URL
        api_url = os.environ.get("YF_LEAGUE_API_URL", "")
        if api_url:
            league = self._data.get("league", {})
            league["api_url"] = api_url
            self._data["league"] = league

    def get_action(self, action_name):
        """Returns action config dict with autonomy, schedule, etc.

        Falls back to DEFAULT_ACTION values for any missing keys.
        """
        actions = self._data.get("actions", {})
        action = actions.get(action_name, {})
        if not action:
            return dict(DEFAULT_ACTION)
        result = dict(DEFAULT_ACTION)
        result.update(action)
        return result

    def get_autonomy(self, action_name):
        """Returns autonomy level string for an action.

        Returns 'off' if the action is not configured.
        """
        action = self.get_action(action_name)
        level = action.get("autonomy", "off")
        if level not in VALID_AUTONOMY_LEVELS:
            return "off"
        return level

    def should_execute(self, action_name):
        """Returns True if autonomy is 'auto'."""
        return self.get_autonomy(action_name) == "auto"

    def should_suggest(self, action_name):
        """Returns True if autonomy is 'auto' or 'suggest'."""
        return self.get_autonomy(action_name) in ("auto", "suggest")

    def should_alert(self, action_name):
        """Returns True if autonomy is 'auto', 'suggest', or 'alert'."""
        return self.get_autonomy(action_name) in ("auto", "suggest", "alert")

    def is_enabled(self, action_name):
        """Returns True if the action is not 'off'."""
        return self.get_autonomy(action_name) != "off"

    def get_notification_config(self):
        """Returns notification preferences with defaults."""
        notif = self._data.get("notifications", {})
        result = dict(DEFAULT_NOTIFICATIONS)
        result.update(notif)
        return result

    def get_api_url(self):
        """Returns API base URL from league config."""
        league = self._data.get("league", {})
        return league.get("api_url", DEFAULT_LEAGUE.get("api_url"))

    def list_actions(self):
        """Returns a list of all configured action names."""
        return list(self._data.get("actions", {}).keys())

    def __repr__(self):
        action_count = str(len(self._data.get("actions", {})))
        return "<AutomationConfig path=" + str(self._path) + " actions=" + action_count + ">"
