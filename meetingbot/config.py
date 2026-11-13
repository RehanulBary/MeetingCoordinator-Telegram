from dataclasses import dataclass
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def read_env(path: Path = Path('.env')) -> dict:
    values = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env(path: Path = Path('.env')) -> None:
    for key, value in read_env(path).items():
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    database: str = 'data/meetingbot.sqlite3'
    timezone: str = 'Asia/Dhaka'
    model: str = 'gpt-6-luna'
    openai_key: str = ''
    telegram_token: str = ''
    bot_username: str = ''
    daily_calls: int = 100
    work_start: int = 9
    work_end: int = 18
    workdays: str = '0,1,2,3,4,5,6'

    @classmethod
    def from_env(cls):
        return cls.from_values({**read_env(), **os.environ})

    @classmethod
    def from_values(cls, values):
        result = cls(
            database=values.get('DATABASE_PATH', cls.database),
            timezone=values.get('COMPANY_TIMEZONE', cls.timezone),
            model=values.get('OPENAI_MODEL', cls.model),
            openai_key=values.get('OPENAI_API_KEY', ''),
            telegram_token=values.get('TELEGRAM_BOT_TOKEN', ''),
            bot_username=values.get('TELEGRAM_BOT_USERNAME', ''),
            daily_calls=int(values.get('MAX_AI_CALLS_PER_DAY', '100')),
            work_start=int(values.get('WORKDAY_START_HOUR', '9')),
            work_end=int(values.get('WORKDAY_END_HOUR', '18')),
            workdays=values.get('WORKDAYS', '0,1,2,3,4,5,6'),
        )
        ZoneInfo(result.timezone)
        if not (0 <= result.work_start < result.work_end <= 24):
            raise ValueError('Configure workday hours with 0 <= start < end <= 24.')
        if not result.workdays or any(int(day) not in range(7) for day in result.workdays.split(',')):
            raise ValueError('WORKDAYS must list weekday numbers 0 (Monday) through 6 (Sunday).')
        if not 1 <= result.daily_calls <= 100000:
            raise ValueError('Daily AI call limit must be between 1 and 100,000.')
        return result
