from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class AuthenticationShellTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='author',
            password='safe-password',
        )

    def test_dashboard_requires_authentication(self):
        response = self.client.get(reverse('accounts:dashboard'))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('accounts:dashboard')}",
        )

    def test_user_can_log_in_and_see_dashboard(self):
        response = self.client.post(
            reverse('accounts:login'),
            {'username': 'author', 'password': 'safe-password'},
        )

        self.assertRedirects(response, reverse('accounts:dashboard'))
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertContains(response, 'Welcome back, author')
        self.assertContains(response, 'No courses yet')

    def test_user_can_log_out_with_a_post_request(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse('accounts:logout'))

        self.assertRedirects(response, reverse('accounts:login'))
        self.assertNotIn('_auth_user_id', self.client.session)
