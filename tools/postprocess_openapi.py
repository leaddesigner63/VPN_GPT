"""Post-process the generated OpenAPI schema and publish the curated VPN_GPT specification."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HTTP_METHODS = {"get", "post"}
EXPECTED_PATHS: dict[str, set[str]] = {
    "/vpn/issue_key": {"post"},
    "/vpn/renew_key": {"post"},
    "/vpn/disable_key": {"post"},
    "/vpn/active": {"get"},
    "/users/": {"get"},
    "/users/expiring": {"get"},
    "/users/userinfo": {"get"},
    "/users/all": {"get"},
    "/notify/send": {"post"},
    "/notify/broadcast": {"post"},
    "/admin/backup_db": {"post"},
    "/admin/stats": {"get"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish the curated OpenAPI schema")
    parser.add_argument("input", type=Path, help="Path to the raw OpenAPI JSON file")
    parser.add_argument("output", type=Path, help="Path to the processed OpenAPI JSON file")
    return parser.parse_args()


def validate_raw_schema(raw: dict[str, Any]) -> None:
    paths: dict[str, Any] = raw.get("paths", {})
    missing: list[str] = []
    for expected_path, expected_methods in EXPECTED_PATHS.items():
        path_item = paths.get(expected_path, {})
        available = {method for method in path_item if method in HTTP_METHODS}
        if not expected_methods.issubset(available):
            missing.append(expected_path)
    if missing:
        raise KeyError(
            "Отсутствуют ожидаемые маршруты в openapi_raw.json: " + ", ".join(sorted(missing))
        )


def build_schema() -> dict[str, Any]:
    error_response = {
        "type": "object",
        "description": "Стандартный ответ об ошибке.",
        "properties": {
            "ok": {"type": "boolean", "const": False},
            "error": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["ok", "error"],
        "additionalProperties": False,
    }

    vpn_issue_key_response = {
        "type": "object",
        "description": "Ответ с данными нового VPN-ключа.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "link": {"type": "string", "description": "Сформированная VLESS-ссылка для подключения."},
            "uuid": {"type": "string", "format": "uuid", "description": "Уникальный идентификатор ключа."},
            "expires": {"type": "string", "format": "date", "description": "Дата истечения срока действия ключа."},
            "message": {"type": "string", "description": "Сообщение для пользователя."},
        },
        "required": ["ok", "link", "uuid", "expires", "message"],
        "additionalProperties": False,
    }

    vpn_renew_key_response = {
        "type": "object",
        "description": "Ответ при продлении VPN-ключа.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "username": {"type": "string"},
            "expires": {"type": "string", "format": "date"},
        },
        "required": ["ok", "username", "expires"],
        "additionalProperties": False,
    }

    vpn_disable_key_response = {
        "type": "object",
        "description": "Ответ при деактивации ключа.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "uuid": {"type": "string", "format": "uuid"},
        },
        "required": ["ok", "uuid"],
        "additionalProperties": False,
    }

    vpn_active_keys_response = {
        "type": "object",
        "description": "Список активных VPN-ключей.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "active": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string"},
                        "uuid": {"type": "string", "format": "uuid"},
                        "link": {"type": "string"},
                        "issued_at": {"type": "string", "format": "date"},
                        "expires_at": {"type": "string", "format": "date"},
                    },
                    "required": ["username", "uuid", "link", "issued_at", "expires_at"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["ok", "active"],
        "additionalProperties": False,
    }

    vpn_key_record = {
        "type": "object",
        "description": "Запись о VPN-ключе в базе данных.",
        "properties": {
            "id": {"type": "integer"},
            "user_id": {"type": ["string", "null"]},
            "username": {"type": ["string", "null"]},
            "uuid": {"type": ["string", "null"]},
            "link": {"type": ["string", "null"]},
            "issued_at": {"type": ["string", "null"]},
            "expires_at": {"type": ["string", "null"]},
            "active": {"type": ["integer", "null"]},
        },
    }

    users_collection_response = {
        "type": "object",
        "description": "Список пользователей VPN.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "users": {"type": "array", "items": vpn_key_record},
            "total": {"type": "integer", "minimum": 0},
        },
        "required": ["ok", "users", "total"],
        "additionalProperties": False,
    }

    userinfo_response = {
        "type": "object",
        "description": "Агрегированные данные по пользователю.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "user": {
                "type": "object",
                "properties": {
                    "username": {"type": ["string", "null"]},
                    "uuid": {"type": ["string", "null"]},
                    "expires_at": {"type": ["string", "null"]},
                    "link": {"type": ["string", "null"]},
                    "chat_id": {"type": ["integer", "null"]},
                    "user_id": {"type": ["string", "null"]},
                    "issued_at": {"type": ["string", "null"]},
                    "active": {"type": ["integer", "null"]},
                },
            },
        },
        "required": ["ok", "user"],
        "additionalProperties": False,
    }

    telegram_users_response = {
        "type": "object",
        "description": "Список Telegram-пользователей.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "users": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "username": {"type": ["string", "null"]},
                        "chat_id": {"type": ["integer", "null"]},
                    },
                    "required": ["username", "chat_id"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["ok", "users"],
        "additionalProperties": False,
    }

    notify_send_response = {
        "type": "object",
        "description": "Результат отправки личного уведомления.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "message": {"type": "string"},
        },
        "required": ["ok", "message"],
        "additionalProperties": False,
    }

    notify_broadcast_response = {
        "type": "object",
        "description": "Результат массовой рассылки.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "message": {"type": "string"},
        },
        "required": ["ok", "message"],
        "additionalProperties": False,
    }

    admin_backup_response = {
        "type": "object",
        "description": "Информация о резервной копии базы данных.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "backup_id": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
            "location": {"type": "string", "format": "uri"},
            "message": {"type": "string"},
        },
        "required": ["ok", "backup_id", "created_at", "location", "message"],
        "additionalProperties": False,
    }

    admin_stats_response = {
        "type": "object",
        "description": "Статистика по пользователям и ключам.",
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "active_keys": {"type": "integer", "minimum": 0},
            "expired_keys": {"type": "integer", "minimum": 0},
            "users_total": {"type": "integer", "minimum": 0},
        },
        "required": ["ok", "active_keys", "expired_keys", "users_total"],
        "additionalProperties": False,
    }

    security_scheme = {
        "AdminTokenAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "x-admin-token",
            "description": "Административный токен, который необходимо передавать в заголовке x-admin-token",
        }
    }

    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "VPN_GPT Unified API",
            "version": "2.1.0",
            "description": "Полный набор API-методов VPN_GPT, синхронизированный с реальным кодом FastAPI",
        },
        "servers": [{"url": "https://vpn-gpt.store/api"}],
        "paths": {
            "/vpn/issue_key": {
                "post": {
                    "tags": ["vpn"],
                    "summary": "Выдаёт VPN-ключ",
                    "description": "Создаёт новый VPN-ключ и возвращает ссылку подключения.",
                    "operationId": "vpnPostIssueKey",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["username"],
                                    "properties": {
                                        "username": {
                                            "type": "string",
                                            "description": "Имя пользователя, для которого создаётся ключ.",
                                        },
                                        "days": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "default": 30,
                                            "description": "Количество дней действия ключа.",
                                        },
                                    },
                                    "additionalProperties": False,
                                },
                                "example": {"username": "demo_user", "days": 30},
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Ключ успешно создан.",
                            "content": {"application/json": {"schema": vpn_issue_key_response}},
                        },
                        "400": {
                            "description": "Некорректные входные данные.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "409": {
                            "description": "У пользователя уже есть активный ключ.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/vpn/renew_key": {
                "post": {
                    "tags": ["vpn"],
                    "summary": "Продлевает VPN-ключ",
                    "description": "Продлевает срок действия существующего VPN-ключа пользователя.",
                    "operationId": "vpnPostRenewKey",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["username"],
                                    "properties": {
                                        "username": {
                                            "type": "string",
                                            "description": "Имя пользователя с действующим ключом.",
                                        },
                                        "days": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "default": 30,
                                            "description": "Дополнительное количество дней.",
                                        },
                                    },
                                    "additionalProperties": False,
                                },
                                "example": {"username": "demo_user", "days": 14},
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Срок действия ключа обновлён.",
                            "content": {"application/json": {"schema": vpn_renew_key_response}},
                        },
                        "400": {
                            "description": "Некорректные входные данные.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "404": {
                            "description": "Активный ключ не найден.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "500": {
                            "description": "Не удалось обновить срок действия ключа.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/vpn/disable_key": {
                "post": {
                    "tags": ["vpn"],
                    "summary": "Деактивирует ключ",
                    "description": "Переводит VPN-ключ в статус неактивного и удаляет клиента из Xray.",
                    "operationId": "vpnPostDisableKey",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["uuid"],
                                    "properties": {
                                        "uuid": {
                                            "type": "string",
                                            "format": "uuid",
                                            "description": "UUID ключа, который требуется отключить.",
                                        }
                                    },
                                    "additionalProperties": False,
                                },
                                "example": {"uuid": "9ad5d1a9-1f6a-4f7b-9f3c-45ab2a0a6c92"},
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Ключ успешно отключён.",
                            "content": {"application/json": {"schema": vpn_disable_key_response}},
                        },
                        "400": {
                            "description": "Не указан UUID ключа.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "404": {
                            "description": "Ключ не найден.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/vpn/active": {
                "get": {
                    "tags": ["vpn"],
                    "summary": "Возвращает список активных ключей",
                    "description": "Предоставляет перечень всех активных VPN-ключей.",
                    "operationId": "vpnGetActive",
                    "responses": {
                        "200": {
                            "description": "Список активных ключей получен.",
                            "content": {"application/json": {"schema": vpn_active_keys_response}},
                        }
                    },
                }
            },
            "/users/": {
                "get": {
                    "tags": ["users"],
                    "summary": "Возвращает всех пользователей",
                    "description": "Список пользователей VPN из таблицы vpn_keys с возможностью фильтрации.",
                    "operationId": "usersGetList",
                    "parameters": [
                        {
                            "name": "username",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Фильтр по имени пользователя.",
                        },
                        {
                            "name": "active",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                            "description": "Фильтр по признаку активности ключа.",
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 500},
                            "description": "Ограничение количества возвращаемых записей.",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Пользователи успешно получены.",
                            "content": {"application/json": {"schema": users_collection_response}},
                        },
                        "404": {
                            "description": "Пользователи не найдены.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "409": {
                            "description": "Неверно указан параметр limit.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/users/expiring": {
                "get": {
                    "tags": ["users"],
                    "summary": "Возвращает пользователей с истекающим сроком",
                    "description": "Показывает пользователей, у которых срок действия VPN-ключа истекает в ближайшие дни.",
                    "operationId": "usersGetExpiring",
                    "parameters": [
                        {
                            "name": "days",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 0, "default": 3},
                            "description": "Количество дней до истечения срока.",
                        },
                        {
                            "name": "username",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Фильтр по имени пользователя.",
                        },
                        {
                            "name": "active",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                            "description": "Фильтр по активности ключа.",
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 500},
                            "description": "Ограничение количества записей.",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Список пользователей с истекающими ключами получен.",
                            "content": {"application/json": {"schema": users_collection_response}},
                        },
                        "404": {
                            "description": "Подходящие пользователи не найдены.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "409": {
                            "description": "Неверно указаны параметры фильтрации.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/users/userinfo": {
                "get": {
                    "tags": ["users"],
                    "summary": "Возвращает объединённые данные пользователя",
                    "description": "Агрегированные данные по VPN, истории и Telegram-чату для выбранного пользователя.",
                    "operationId": "usersGetUserinfo",
                    "parameters": [
                        {
                            "name": "username",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "Имя пользователя для поиска.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Объединённые данные пользователя получены.",
                            "content": {"application/json": {"schema": userinfo_response}},
                        },
                        "404": {
                            "description": "Пользователь не найден.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "409": {
                            "description": "Не указан username пользователя.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/users/all": {
                "get": {
                    "tags": ["users"],
                    "summary": "Возвращает всех с chat_id",
                    "description": "Выводит всех Telegram-пользователей из локальной таблицы tg_users.",
                    "operationId": "usersGetAll",
                    "responses": {
                        "200": {
                            "description": "Список Telegram-пользователей получен.",
                            "content": {"application/json": {"schema": telegram_users_response}},
                        }
                    },
                }
            },
            "/notify/send": {
                "post": {
                    "tags": ["notify"],
                    "summary": "Отправить сообщение пользователю",
                    "description": "Отправляет личное уведомление пользователю в Telegram.",
                    "operationId": "notifyPostSend",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["username", "text"],
                                    "properties": {
                                        "username": {
                                            "type": "string",
                                            "description": "Имя пользователя в Telegram.",
                                        },
                                        "text": {
                                            "type": "string",
                                            "description": "Текст уведомления.",
                                        },
                                    },
                                    "additionalProperties": False,
                                },
                                "example": {
                                    "username": "demo_user",
                                    "text": "Ваш ключ продлён до 2024-12-31.",
                                },
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Уведомление отправлено.",
                            "content": {"application/json": {"schema": notify_send_response}},
                        },
                        "400": {
                            "description": "Не указаны необходимые поля.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "404": {
                            "description": "Не удалось найти chat_id пользователя.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "409": {
                            "description": "Telegram вернул ошибку при отправке сообщения.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "500": {
                            "description": "Ошибка взаимодействия с Telegram API.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/notify/broadcast": {
                "post": {
                    "tags": ["notify"],
                    "summary": "Массовая рассылка",
                    "description": "Отправляет сообщение всем пользователям из таблицы tg_users.",
                    "operationId": "notifyPostBroadcast",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {
                                        "text": {
                                            "type": "string",
                                            "description": "Текст сообщения для массовой рассылки.",
                                        }
                                    },
                                    "additionalProperties": False,
                                },
                                "example": {"text": "Сегодня плановые технические работы в 23:00."},
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Рассылка выполнена.",
                            "content": {"application/json": {"schema": notify_broadcast_response}},
                        },
                        "400": {
                            "description": "Пустой текст сообщения.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "500": {
                            "description": "BOT_TOKEN не настроен или произошла ошибка при отправке.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/admin/backup_db": {
                "post": {
                    "tags": ["admin"],
                    "summary": "Резервное копирование БД",
                    "description": "Создаёт резервную копию базы данных и возвращает информацию о файле.",
                    "operationId": "adminPostBackupDb",
                    "responses": {
                        "200": {
                            "description": "Резервная копия создана.",
                            "content": {"application/json": {"schema": admin_backup_response}},
                        }
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
            "/admin/stats": {
                "get": {
                    "tags": ["admin"],
                    "summary": "Статистика по пользователям и ключам",
                    "description": "Возвращает агрегированные показатели по активным и просроченным ключам.",
                    "operationId": "adminGetStats",
                    "parameters": [
                        {
                            "name": "username",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Фильтр по имени пользователя.",
                        },
                        {
                            "name": "active",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                            "description": "Фильтр по признаку активности ключей.",
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 500},
                            "description": "Ограничение количества записей при вычислении статистики.",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Статистика успешно вычислена.",
                            "content": {"application/json": {"schema": admin_stats_response}},
                        },
                        "404": {
                            "description": "Статистика недоступна для выбранных параметров.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                        "409": {
                            "description": "Неверно указан параметр limit.",
                            "content": {"application/json": {"schema": error_response}},
                        },
                    },
                    "security": [{"AdminTokenAuth": []}],
                }
            },
        },
        "components": {
            "schemas": {
                "ErrorResponse": error_response,
                "VpnIssueKeyResponse": vpn_issue_key_response,
                "VpnRenewKeyResponse": vpn_renew_key_response,
                "VpnDisableKeyResponse": vpn_disable_key_response,
                "VpnActiveKeysResponse": vpn_active_keys_response,
                "VpnKeyRecord": vpn_key_record,
                "UsersCollectionResponse": users_collection_response,
                "UserinfoResponse": userinfo_response,
                "TelegramUsersResponse": telegram_users_response,
                "NotifySendResponse": notify_send_response,
                "NotifyBroadcastResponse": notify_broadcast_response,
                "AdminBackupResponse": admin_backup_response,
                "AdminStatsResponse": admin_stats_response,
            },
            "securitySchemes": security_scheme,
        },
    }

    return schema


def main() -> None:
    args = parse_args()
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    validate_raw_schema(raw)
    processed = build_schema()
    args.output.write_text(json.dumps(processed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
