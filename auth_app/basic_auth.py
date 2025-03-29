import httpx
import redis.asyncio as redis
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, exceptions
from rest_framework.authentication import BasicAuthentication
from rest_framework.permissions import AllowAny
from asgiref.sync import sync_to_async

from .models import AuthUser
from .serializers import RegisterUserSerializer, AuthUserSerializer
from .security import verify_password, hash_password
from . import crud

# Подключение к Redis
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# failed attempts
MAX_ATTEMPTS = 5
BLOCK_TIME_SECONDS = 300  # 5 минут


class BasicAuthCredentialsAPIView(APIView):
    authentication_classes = [BasicAuthentication]
    permission_classes = [AllowAny]

    async def get(self, request):
        user = request.user
        return Response({
            "message": "Hi!",
            "username": user.username if user and hasattr(user, "username") else "Anonymous",
            "password": request.META.get("HTTP_AUTHORIZATION", "")
        })


class RegisterUserAPIView(APIView):
    permission_classes = [AllowAny]

    async def post(self, request):
        serializer = RegisterUserSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )
        user_data = serializer.validated_data

        # 1 Запрос на создание
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.user_service_url}/api/v1/users/create_user",
                json={
                    "username": user_data.username,
                    "email": user_data.email,
                }
            )

        if response.status_code not in (200, 201):
            print(f"response.status_code: {response.status_code}")
            return Response(
                {"detail": "Failed to create user profile in user_service"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        user_profile = response.json()
        user_id = user_profile.get("id")
        if not user_id:
            return Response(
                {"detail": "User profile creation error: no user_id returned"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # 2 Хешируем пароль и создаем запись в auth_service
        hashed_pw = hash_password(user_data["password"])
        new_auth_user = await sync_to_async(AuthUser.objects.create)(
            user_id=user_id,
            password=hashed_pw,
            refresh_token=None,
        )
        return Response(
            {"message": "User registered successfully", "user_id": user_id},
            status=status.HTTP_201_CREATED
        )


class GetUsersAPIView(APIView):
    async def get(self):
        users = await sync_to_async(crud.get_all_users)()
        serializer = AuthUserSerializer(users, many=True)
        return Response(serializer.data)


# Вспомогательная асинхронная функция для аутентификации по basic auth
async def get_auth_user_username(request):
    auth = BasicAuthentication()
    try:
        user, _ = auth.authenticate(request=request)
    except exceptions.AuthenticationFailed:
        raise exceptions.AuthenticationFailed("Invalid username or password")
    username = user.username

    unauthed_exc = exceptions.AuthenticationFailed(
        "Invalid username or password",
    )

    # Проверка попыток входа через redis
    key = f"failed_attempts:{username}"
    attempts = await redis_client.get(key)
    attempts = int(attempts) if attempts else 0

    if attempts >= MAX_ATTEMPTS:
        raise exceptions.PermissionDenied(
            "Too many failed attempts, try again later"
        )

    # Запрос пользователя из user_service
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{settings.user_service_url}/api/v1/users/username/{username}")

    if response.status_code != 200:
        await redis_client.incr(key)
        await redis_client.expire(key, BLOCK_TIME_SECONDS)
        raise unauthed_exc  # Пользователь не найден

    user_data = response.json()
    user_id = user_data.get("id")
    is_active = user_data.get("is_active")

    if not user_id or not is_active:
        await redis_client.incr(key)
        await redis_client.expire(key, BLOCK_TIME_SECONDS)
        raise unauthed_exc

    try:
        auth_user = await sync_to_async(AuthUser.objects.get)(user_id=user_id)
    except AuthUser.DoesNotExist:
        await redis_client.incr(key)
        await redis_client.expire(key, BLOCK_TIME_SECONDS)
        raise unauthed_exc

    hashed_password = auth_user.password

    # secrets
    req_password = request.query_params.get("password") or request.data.get("password", "")
    if not verify_password(req_password, hashed_password):
        await redis_client.incr(key)
        await redis_client.expire(key, BLOCK_TIME_SECONDS)
        raise unauthed_exc

    await redis_client.delete(key)
    return username


class BasicAuthUsernameAPIView(APIView):
    authentication_classes = [BasicAuthentication]
    permission_classes = [AllowAny]

    async def get(self, request):
        username = await get_auth_user_username(request)
        return Response({
            "username": username
        })


def get_username_by_static_auth_token(request):
    static_token = request.headers.get(alias="x-auth-token")
    if static_token and (username := crud.static_auth_token_to_user_id.get(static_token)):
        return username
    raise exceptions.AuthenticationFailed("Invalid token")


class CheckTokenAuthAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        username = get_username_by_static_auth_token(request)
        return Response({
            "message": f"Hi!, {username}!",
            "username": username,
        })


class DeleteAuthUserAPIView(APIView):
    def delete(self, request, user_id: int):
        auth_user = crud.get_auth_user(user_id)
        if not auth_user:
            return Response(
                {"detail": f"User with {user_id} not found in auth_service"},
                status=status.HTTP_404_NOT_FOUND
            )
        crud.delete_auth_user(user_id)
        return Response({"message": "Auth user deleted"})