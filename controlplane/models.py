from ipaddress import ip_network
from typing import Literal, Annotated
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Target(Strict):
    profile: str = Field(min_length=1, max_length=80)
    region: str
    service: Literal['ec2', 'lightsail']
    instance: str = Field(min_length=1, max_length=200)
    # Filled by the server at submission, never inferred from an active UI account.
    account_id: str = ''
    partition: str = ''
    generation: str = ''
    period: Literal[60, 300] = 300
    created_at: str = ''


class FirewallRule(Strict):
    protocol: Literal['tcp', 'udp']
    from_port: int = Field(ge=1, le=65535)
    to_port: int = Field(ge=1, le=65535)
    cidr: str = Field(min_length=1, max_length=64)

    @field_validator('cidr')
    @classmethod
    def normalize_cidr(cls, value):
        return str(ip_network(value.strip(), strict=False))

    @model_validator(mode='after')
    def validate_port_range(self):
        if self.from_port > self.to_port:
            raise ValueError('End port must be at least the start port')
        return self


class Launch(Strict):
    user_data: str = Field(default='', max_length=40000)
    ssh_public_key: str = Field(default='', max_length=12000)
    root_password: str = Field(default='', max_length=256, repr=False)
    root_login: Literal['disabled', 'key', 'password'] = 'disabled'
    password_auth: bool = False
    username: str = Field(default='', pattern=r'^[a-z_][a-z0-9_-]{0,31}$|^$')
    firewall_rules: list[FirewallRule] = Field(default_factory=list, max_length=50)


class CreateOptions(Strict):
    blueprint_id: str = ''
    bundle_id: str = ''
    availability_zone: str = ''
    image_id: str = ''
    instance_type: str = ''
    subnet_id: str = ''
    security_group_ids: list[str] = Field(default_factory=list, max_length=20)
    disk_gib: int = Field(default=20, ge=8, le=16384)
    public_ip: bool = False
    key_pair_name: str = ''
    instance_profile_arn: str = ''
    launch_template_id: str = ''
    launch_template_version: str = ''
    tags: dict[str, str] = Field(default_factory=dict, max_length=40)
    bootstrap_id: str = ''
    launch: Launch = Field(default_factory=Launch)


class JobItem(Strict):
    kind: Literal['create', 'delete', 'start', 'stop', 'reboot', 'script', 'bootstrap_verify']
    target: Target
    options: CreateOptions = Field(default_factory=CreateOptions)
    script_id: str = ''
    connection_id: str = ''
    parameters: dict[Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')], str] = Field(default_factory=dict, max_length=40)


class JobRequest(Strict):
    request_key: str = Field(min_length=8, max_length=100)
    items: list[JobItem] = Field(min_length=1, max_length=100)
    confirm: str = ''
    preview_id: str = ''


class ConfigDocument(Strict):
    kind: Literal['bootstrap', 'script', 'connection', 'rule']
    name: str = Field(min_length=1, max_length=100)
    payload: dict


class BootstrapUpdate(Strict):
    name: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    payload: dict


class Connection(Strict):
    resource_key: str = Field(default='', max_length=500)
    username: str = Field(pattern=r'^[a-z_][a-z0-9_-]{0,31}$')
    port: int = Field(default=22, ge=1, le=65535)
    private_key: str = Field(default='', max_length=20000, repr=False)
    password: str = Field(default='', max_length=512, repr=False)
    fingerprint: str = Field(pattern=r'^SHA256:[A-Za-z0-9+/]{43}$')
    address: Literal['public', 'private'] = 'public'
    sudo: bool = False


class Script(Strict):
    body: str = Field(min_length=1, max_length=40000)
    timeout: int = Field(default=120, ge=1, le=1800)
    idempotent: bool = False
    parameters: dict[Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')], str] = Field(default_factory=dict, max_length=40)


class Selector(Strict):
    profiles: list[str] = Field(default_factory=list, max_length=20)
    region: str = 'all'
    service: Literal['all', 'ec2', 'lightsail'] = 'all'
    tags: dict[str, str] = Field(default_factory=dict, min_length=1, max_length=10)


class Rule(Strict):
    targets: list[Target] = Field(default_factory=list, max_length=100)
    selector: Selector | None = None
    aggregation: Literal['instance', 'group'] = 'instance'
    metric: Literal['inbound', 'outbound', 'total', 'rate_mbps'] = 'total'
    window: Literal['5m', '1h', 'day', 'month'] = '1h'
    threshold: float = Field(gt=0, le=1000000000)
    recovery: float | None = Field(default=None, ge=0)
    consecutive: int = Field(default=2, ge=1, le=100)
    cooldown: int = Field(default=3600, ge=300, le=2592000)
    daily_limit: int = Field(default=3, ge=1, le=100)
    timezone: str = 'UTC'
    period: Literal[60, 300] = 300
    enabled: bool = True
    mode: Literal['notify', 'execute'] = 'notify'
    actions: list[Literal['notify', 'script', 'stop']] = Field(default_factory=lambda: ['notify'], min_length=1, max_length=3)
    script_id: str = ''
    connection_id: str = ''
    script_parameters: dict[Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')], str] = Field(default_factory=dict, max_length=40)
    maintenance_until: str = ''
