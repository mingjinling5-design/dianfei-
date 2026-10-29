import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class Account(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    user=models.OneToOneField(settings.AUTH_USER_MODEL,null=True,on_delete=models.CASCADE)
    provider_uid=models.CharField(max_length=100,unique=True,null=True)
    encrypted_state=models.TextField(default='')
    config=models.JSONField(default=dict)
    next_collect=models.DateTimeField(default=timezone.now)
    last_result=models.JSONField(default=dict)
    created_at=models.DateTimeField(auto_now_add=True)


class Room(models.Model):
    owner=models.ForeignKey(Account,on_delete=models.CASCADE)
    room_key=models.CharField(max_length=128)
    enabled=models.BooleanField(default=True)
    data=models.JSONField(default=dict)
    class Meta:constraints=[models.UniqueConstraint(fields=['owner','room_key'],name='owner_room')]


class Reading(models.Model):
    owner=models.ForeignKey(Account,on_delete=models.CASCADE)
    room_key=models.CharField(max_length=128)
    ts=models.CharField(max_length=40)
    balance_kwh=models.FloatField(null=True)
    balance_cny=models.FloatField(null=True)
    source=models.CharField(max_length=32,default='eschool')
    raw=models.JSONField(null=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['owner','room_key','ts'],name='owner_reading')]
        indexes=[models.Index(fields=['owner','room_key','ts'])]


class Event(models.Model):
    owner=models.ForeignKey(Account,on_delete=models.CASCADE)
    room_key=models.CharField(max_length=128)
    ts=models.CharField(max_length=40)
    kind=models.CharField(max_length=60)
    value=models.FloatField(default=0)
    note=models.TextField(default='')
    class Meta:constraints=[models.UniqueConstraint(fields=['owner','room_key','ts','kind'],name='owner_event')]


class Alert(models.Model):
    owner=models.ForeignKey(Account,on_delete=models.CASCADE)
    room_key=models.CharField(max_length=128)
    ts=models.CharField(max_length=40)
    level=models.CharField(max_length=30)
    message=models.TextField()
    acked=models.BooleanField(default=False)


class Metadata(models.Model):
    owner=models.ForeignKey(Account,on_delete=models.CASCADE)
    key=models.CharField(max_length=100)
    value=models.JSONField(null=True)
    class Meta:constraints=[models.UniqueConstraint(fields=['owner','key'],name='owner_metadata')]


class RateLimit(models.Model):
    key=models.CharField(max_length=64,primary_key=True)
    started=models.DateTimeField(default=timezone.now)
    count=models.PositiveIntegerField(default=0)
