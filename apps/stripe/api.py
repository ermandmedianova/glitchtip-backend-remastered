from django.http import JsonResponse
from django.shortcuts import aget_object_or_404
from ninja import ModelSchema, Router

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization
from apps.organizations_ext.tasks import check_organization_throttle
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.schema import CamelSchema
from glitchtip.utils import async_call_celery_task

from .client import (
    create_customer,
    create_portal_session,
    create_session,
    create_subscription,
)
from .constants import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    CollectionMethod,
    SubscriptionStatus,
)
from .models import StripePrice, StripeProduct, StripeSubscription
from .utils import unix_to_datetime

router = Router()


class StripeIDSchema(CamelSchema):
    stripe_id: str


class StripeNestedPriceSchema(StripeIDSchema, ModelSchema):
    price: str

    class Meta:
        model = StripePrice
        fields = ["price"]

    @staticmethod
    def resolve_price(obj: StripePrice):
        return str(obj.price)


class StripeProductSchema(StripeIDSchema, ModelSchema):
    class Meta:
        model = StripeProduct
        fields = ["name", "description", "events", "default_price"]


class StripeProductExpandedPriceSchema(StripeIDSchema, ModelSchema):
    default_price: StripeNestedPriceSchema

    class Meta:
        model = StripeProduct
        fields = ["name", "description", "events"]

    @staticmethod
    def resolve_default_price(obj: StripeProduct):
        return obj.default_price


class StripeSubscriptionSchema(StripeIDSchema, ModelSchema):
    product: StripeProductSchema
    price: StripeNestedPriceSchema
    status: SubscriptionStatus | None
    collection_method: CollectionMethod

    class Meta:
        model = StripeSubscription
        fields = ["created", "current_period_start", "current_period_end", "start_date"]

    @staticmethod
    def resolve_price(obj: StripeSubscription):
        return obj.price

    @staticmethod
    def resolve_product(obj: StripeSubscription):
        return obj.price.product


class PriceIDSchema(CamelSchema):
    price: str


class SubscriptionIn(PriceIDSchema):
    organization: str


class CreateSubscriptionResponse(SubscriptionIn):
    subscription: StripeSubscriptionSchema


class StripeCheckoutSessionSchema(CamelSchema):
    id: str


class StripePortalSessionSchema(CamelSchema):
    url: str


class EventsCountSchema(CamelSchema):
    event_count: int
    transaction_event_count: int
    uptime_check_event_count: int
    file_size_mb: int


@router.get("products/", response=list[StripeProductExpandedPriceSchema], by_alias=True)
async def list_stripe_products(request: AuthHttpRequest):
    return [
        product
        async for product in StripeProduct.objects.filter(
            is_public=True, events__gt=0
        ).select_related("default_price")
    ]


@router.get(
    "subscriptions/{slug:organization_slug}/",
    response=StripeSubscriptionSchema | None,
    by_alias=True,
)
async def get_stripe_subscription(request: AuthHttpRequest, organization_slug: str):
    return await (
        StripeSubscription.objects.filter(
            organization__users=request.auth.user_id,
            organization__slug=organization_slug,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        )
        .select_related("price__product")
        .order_by("-created")
        .afirst()
    )


@router.post(
    "organizations/{slug:organization_slug}/create-stripe-subscription-checkout/",
    response=StripeCheckoutSessionSchema,
)
async def create_stripe_session(
    request: AuthHttpRequest, organization_slug: str, payload: PriceIDSchema
):
    """
    Create Stripe Checkout, send to client for redirecting to Stripe
    See https://stripe.com/docs/api/checkout/sessions/create
    """
    organization = await aget_object_or_404(
        Organization.objects.select_related("owner__organization_user__user"),
        slug=organization_slug,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    # Ensure price exists
    price_id = payload.price
    await aget_object_or_404(StripePrice, stripe_id=price_id)
    return await create_session(price_id, customer_id, organization_slug)


@router.post(
    "organizations/{slug:organization_slug}/create-billing-portal/",
    response=StripePortalSessionSchema,
)
async def stripe_billing_portal_session(
    request: AuthHttpRequest, organization_slug: str
):
    """See https://stripe.com/docs/billing/subscriptions/integrating-self-serve-portal"""
    organization = await aget_object_or_404(
        Organization.objects.select_related("owner__organization_user__user"),
        slug=organization_slug,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    return await create_portal_session(customer_id, organization_slug)


@router.post("subscriptions/", response=CreateSubscriptionResponse, by_alias=True)
async def stripe_create_subscription(request: AuthHttpRequest, payload: SubscriptionIn):
    org_id = int(payload.organization)
    organization = await aget_object_or_404(
        Organization.objects.select_related("owner__organization_user__user"),
        id=org_id,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    price = await aget_object_or_404(
        StripePrice.objects.select_related("product"), stripe_id=payload.price, price=0
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    if await StripeSubscription.objects.filter(
        organization=organization, status__in=ACTIVE_SUBSCRIPTION_STATUSES
    ).aexists():
        return JsonResponse({"detail": "Customer already has subscription"}, status=400)
    subscription_resp = await create_subscription(customer_id, price.stripe_id)
    subscription = await StripeSubscription.objects.acreate(
        stripe_id=subscription_resp.id,
        status=SubscriptionStatus.ACTIVE,
        created=unix_to_datetime(subscription_resp.created),
        current_period_start=unix_to_datetime(
            subscription_resp.items.data[0].current_period_start
        ),
        current_period_end=unix_to_datetime(
            subscription_resp.items.data[0].current_period_end
        ),
        start_date=unix_to_datetime(subscription_resp.start_date),
        collection_method=subscription_resp.collection_method,
        price=price,
        organization=organization,
    )
    organization.stripe_primary_subscription = subscription
    await organization.asave(update_fields=["stripe_primary_subscription"])
    await async_call_celery_task(check_organization_throttle, organization.id)
    return {
        "price": price.stripe_id,
        "organization": str(organization.id),
        "subscription": subscription,
    }


@router.get(
    "subscriptions/{slug:organization_slug}/events_count/",
    response=EventsCountSchema,
    by_alias=True,
)
async def subscription_events_count(request: AuthHttpRequest, organization_slug: str):
    org = await aget_object_or_404(
        Organization.objects.with_event_counts(),
        slug=organization_slug,
        users=request.auth.user_id,
    )
    return {
        "event_count": org.issue_event_count,
        "transaction_event_count": org.transaction_count,
        "uptime_check_event_count": org.uptime_check_event_count,
        "file_size_mb": org.file_size,
    }
