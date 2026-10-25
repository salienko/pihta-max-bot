"""Explicit extension points. No optional business features ship in rental v3.

A feature owns feature:<key>:... callbacks, its state-filtered message handlers,
its tables/migrations, and its tests. It may read rental via BookingStore.get;
submitted rental snapshots are never rewritten by extensions.
"""
from dataclasses import dataclass
import re
from aiogram import F, Router
from aiogram.types import BotCommand

CORE_COMMANDS = (
    BotCommand(command='start',description='Главное меню'),
    BotCommand(command='book',description='Забронировать баню'),
    BotCommand(command='bookings',description='Мои бронирования'),
    BotCommand(command='help',description='Помощь'),
)

@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    router: Router
    commands: tuple = ()

    @property
    def entry(self): return 'feature:'+self.key+':home'


class Features:
    def __init__(self, features=()):
        self.items=tuple(features)
        keys=set();commands={c.command for c in CORE_COMMANDS}|{'admin','id','menu'}
        routers=set()
        for feature in self.items:
            if not re.fullmatch('[a-z][a-z0-9_]{0,15}',feature.key) or feature.key in keys:
                raise ValueError('Feature key must be unique, short ASCII')
            if id(feature.router) in routers:raise ValueError('Each feature needs its own router')
            if not 1<=len(feature.title)<=40:raise ValueError('Use a short menu title')
            keys.add(feature.key);routers.add(id(feature.router))
            for command in feature.commands:
                if command.command in commands:raise ValueError('Duplicate bot command')
                commands.add(command.command)

    def menu(self): return [[(feature.title,feature.entry)] for feature in self.items]
    def commands(self):return [*CORE_COMMANDS,*(command for feature in self.items for command in feature.commands)]

    def mount(self,dispatcher,core):
        for feature in self.items:
            feature.router.callback_query.filter(F.data.startswith('feature:'+feature.key+':'))
            dispatcher.include_router(feature.router)
        dispatcher.include_router(core)


def configured_features(settings,store):
    # Add a factory here only when that feature and its migrations pass their tests.
    return Features()
