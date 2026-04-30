import json

def get_main_flow():
    return [
        {
            "name": "Welcome",
            "preAction": [
                {
                    "text": "Hi! Welcome to the Corestack Chatbot.\n1 - Weather Forecast\n2 - Crop Advisory\n3 - Join Community"
                }
            ],
            "postAction": [],
            "transition": [
                {"WeatherMenu": ["1"]},
                {"CropAdvisoryMenu": ["2"]},
                {"JoinCommunityStart": ["3"]}
            ]
        },
        {
            "name": "WeatherMenu",
            "preAction": [
                {
                    "text": "Weather Forecast Options:\n1 - Get Current Weather Forecast\n2 - 5 days ForeCast\n3 - Back to Main Menu"
                }
            ],
            "postAction": [],
            "transition": [
                {"WeatherLocation": ["1", "2"]},
                {"Welcome": ["3"]}
            ]
        },
        {
            "name": "WeatherLocation",
            "preAction": [
                {"text": "Please provide your location."}
            ],
            "postAction": [
                {"function": "handle_weather_forecast"}
            ],
            "transition": [
                {"Welcome": ["success"]}
            ]
        },
        {
            "name": "CropAdvisoryMenu",
            "preAction": [
                {"text": "Enter the Crop Name and Crop Sowing Date (e.g., Wheat 2026-01-15)"}
            ],
            "postAction": [
                {"function": "store_crop_data"}
            ],
            "transition": [
                {"CropLocation": ["success"]}
            ]
        },
        {
            "name": "CropLocation",
            "preAction": [
                {"text": "Please provide your location."}
            ],
            "postAction": [
                {"function": "handle_crop_advisory"}
            ],
            "transition": [
                {"Welcome": ["success"]}
            ]
        },
        {
            "name": "JoinCommunityStart",
            "preAction": [
                {"text": "Please provide your location to check for local villages."}
            ],
            "postAction": [
                {"function": "check_villages_by_location"}
            ],
            "transition": [
                {"VillageSelection": ["registered", "new_user"]}
            ]
        },
        {
            "name": "VillageSelection",
            "preAction": [
                {"function": "display_village_list"}
            ],
            "postAction": [
                {"function": "handle_village_selection"}
            ],
            "transition": [
                {"CommunityMenu": ["success"]}
            ]
        },
        {
            "name": "CommunityMenu",
            "preAction": [
                {
                    "text": "Choose from the below:\n1 - Create Asset Demand\n2 - Create Story\n3 - View Demands\n4 - View Stories\n5 - Back to Main"
                }
            ],
            "postAction": [],
            "transition": [
                {"AssetDemandFlow": ["1"]},
                {"CreateStoryFlow": ["2"]},
                {"ViewDemandsFlow": ["3"]},
                {"ViewStoriesFlow": ["4"]},
                {"Welcome": ["5"]}
            ]
        }
        # ... more states for Asset/Story flows ...
    ]

# This is a starting point for the SMJ JSON.
