# Things to Keep in Mind when Deploying Django App

## Installation

- Run the install.sh it will make the environment and install all the dependency.
- When installing new Library always install via the environment.yml doc using the following command

```c
conda env update --file environment.yml
```

## Things to Update after deployment

- update the `BPP_URI` environment variable pointing to the deployed Onix server URL
- In the Onix deployment `local-simple-routing-BPPReceiver.yaml` file in the target url variable put the deployed public backend url **till port only** Example below

```C
routingRules:
  - domain: "beckn:dataset:ddm:1.0.0"  # Retail domain
    version: "2.0.0"
    targetType: "url"
    target:
      url: "http://172.17.0.1:8000" 👈
    endpoints:
      - select
      - init
      - confirm
```

- Update the `local_url` to point the deployed public backend url for the `beckn : confirm` call to run

### Environment Variables

```C
AWS_ACCESS_KEY_ID = ""
AWS_SECRET_ACCESS_KEY = ""
AWS_REGION_NAME = "ap-south-1"
X_API_KEY = ""
GEOSERVER_URL = "https://geoserver.core-stack.org:8443/geoserver"
BPP_URI = ""
LOCAL_URL = ""
```
