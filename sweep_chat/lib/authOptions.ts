import { AuthOptions } from 'next-auth'
import CredentialsProvider from 'next-auth/providers/credentials'

const authOptions: AuthOptions = {
  providers: [
    CredentialsProvider({
      name: 'Local Dev',
      credentials: {},
      async authorize() {
        // Always return a mock local user
        return {
          id: '1',
          name: 'Local Developer',
          email: 'local@localhost',
          image: 'https://github.com/ghost.png',
        }
      }
    })
  ],
  callbacks: {
    async session({ session, token }) {
      session.user = {
        name: 'Local Developer',
        username: 'localdev',
        image: 'https://github.com/ghost.png',
        accessToken: 'mock-local-token', // Fake token for backend
        refreshToken: 'mock-local-token',
        expires_at: Date.now() + 1000 * 60 * 60 * 24,
      }
      return session
    }
  }
}
export default authOptions