import { initializeApp } from "https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js";
import { getFirestore, doc, setDoc, getDoc } from "https://www.gstatic.com/firebasejs/10.7.1/firebase-firestore.js";

const firebaseConfig = {
    apiKey: "AIzaSyA4KIj_-KRfUi6RqgKwQwOfJ1ISnVsfWlk",
    authDomain: "kp-stocks-live.firebaseapp.com",
    projectId: "kp-stocks-live",
    storageBucket: "kp-stocks-live.firebasestorage.app",
    messagingSenderId: "935244560149",
    appId: "1:935244560149:web:361a8e8d19f72233339b1c",
    measurementId: "G-BWSLZL4FQR"
};

const app = initializeApp(firebaseConfig);
const db = getFirestore(app);

export { db, doc, setDoc, getDoc };
