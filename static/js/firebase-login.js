(function () {
  const firebaseConfig = {
    apiKey: "AIzaSyA2IqmsUTS55gFdA_3ZV_eMzntkhF2tFJg",
    authDomain: "cloudplatform-assignment.firebaseapp.com",
    projectId: "cloudplatform-assignment",
    storageBucket: "cloudplatform-assignment.firebasestorage.app",
    messagingSenderId: "25474284061",
    appId: "1:25474284061:web:4b5133abc1518fefe1e4f4"
  };

  firebase.initializeApp(firebaseConfig);
  const auth = firebase.auth();
  const googleProvider = new firebase.auth.GoogleAuthProvider();

  async function postSession(idToken) {
    const body = new URLSearchParams();
    body.set("id_token", idToken);
    const res = await fetch("/auth/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body,
      credentials: "same-origin",
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "Session login failed");
    }
    return res.json();
  }

  window.firebaseLogin = {
    async signInWithEmailPassword(email, password) {
      const cred = await auth.signInWithEmailAndPassword(email, password);
      const idToken = await cred.user.getIdToken();
      await postSession(idToken);
      window.location.assign("/");
    },
    async signUpWithEmailPassword(email, password) {
      const cred = await auth.createUserWithEmailAndPassword(email, password);
      const idToken = await cred.user.getIdToken();
      await postSession(idToken);
      window.location.assign("/");
    },
    async signInWithGoogle() {
      const cred = await auth.signInWithPopup(googleProvider);
      const idToken = await cred.user.getIdToken();
      await postSession(idToken);
      window.location.assign("/");
    },
    async signIn(email, password) {
      return this.signInWithEmailPassword(email, password);
    },
    async signOut() {
      await auth.signOut();
      window.location.assign("/auth/logout");
    },
  };

  auth.onAuthStateChanged(async (user) => {
    const el = document.getElementById("auth-status");
    if (el) {
      el.textContent = user ? user.email || "Signed in" : "Signed out";
    }
  });
})();
  