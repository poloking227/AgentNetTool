"""Model response validation and safe, actionable processing errors."""


class ModelPredictionError(RuntimeError):
    error_kind = "model"


TARGET_TEXT_FIELDS = ("title", "value", "description", "role", "role_description")


def is_valid_prediction(result):
    """Accept only target objects with usable text understood by the UI."""
    if (not isinstance(result, dict) or 'error' in result
            or result.get('status') == 'failed' or result.get('success') is False):
        return False
    values = [result[key] for key in TARGET_TEXT_FIELDS if key in result]
    if any(value is not None and not isinstance(value, str) for value in values):
        return False
    return any(isinstance(value, str) and value.strip() for value in values)


def validate_predictions(results, expected):
    if not isinstance(results, list) or len(results) != expected:
        raise ModelPredictionError('Model recognition failed: response result count is invalid. Original recording is preserved. Retry processing.')
    if not all(is_valid_prediction(result) for result in results):
        raise ModelPredictionError('Model recognition failed: response contains invalid results. Original recording is preserved. Retry processing.')
    return results


def request_predictions(url, payload, expected):
    import requests
    try:
        response = requests.post(url, json=payload, timeout=(10, 60))
        response.raise_for_status()
    except requests.Timeout as error:
        raise ModelPredictionError('Model recognition timed out. Original recording is preserved. Check the connection and Retry.') from error
    except requests.HTTPError as error:
        code = error.response.status_code if error.response is not None else 'unknown'
        raise ModelPredictionError(f'Model service returned HTTP {code}. Original recording is preserved. Retry later.') from error
    except requests.RequestException as error:
        raise ModelPredictionError('Cannot connect to the model service. Original recording is preserved. Check the connection and Retry.') from error
    try:
        body = response.json()
    except ValueError as error:
        raise ModelPredictionError('Model recognition failed: service response is not valid JSON. Original recording is preserved. Retry later.') from error
    if not isinstance(body, dict) or body.get('error') or body.get('status') == 'failed':
        raise ModelPredictionError('Model recognition failed: service returned an invalid response. Original recording is preserved. Retry later.')
    return validate_predictions(body.get('results'), expected)
