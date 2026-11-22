"""Built-in viewer language labels."""

WATCH_KEYS = (
    "explore", "feed", "watch_later", "liked_videos", "collections", "groups", "viewer_account",
)
VIEWER_KEYS = ("language", "browser_language", "save_viewing_preferences")
FEED_KEYS = (
    "sign_in_feed", "manage_feed", "unfollow", "sign_in_manage", "videos", "appearances",
    "likes", "save_activity", "empty_follows", "empty_creators", "feed_pages", "previous_page", "next_page",
)

WATCH = {
    "en": ("Explore", "Feed", "Watch Later", "Liked videos", "Collections", "Groups", "Viewer account"),
    "fr": ("Explorer", "Fil", "À regarder plus tard", "Vidéos aimées", "Collections", "Groupes", "Compte du spectateur"),
    "es": ("Explorar", "Feed", "Ver más tarde", "Vídeos que me gustan", "Colecciones", "Grupos", "Cuenta del espectador"),
    "de": ("Entdecken", "Feed", "Später ansehen", "Mit „Gefällt mir“ markierte Videos", "Sammlungen", "Gruppen", "Zuschauerkonto"),
    "pt-BR": ("Explorar", "Feed", "Assistir mais tarde", "Vídeos curtidos", "Coleções", "Grupos", "Conta do espectador"),
    "ko": ("탐색", "피드", "나중에 볼 동영상", "좋아요 표시한 동영상", "컬렉션", "그룹", "시청자 계정"),
    "ja": ("探索", "フィード", "後で見る", "高評価した動画", "コレクション", "グループ", "視聴者アカウント")
}
VIEWER = {
    "en": ("Language", "Use browser language", "Save viewing preferences"),
    "fr": ("Langue", "Utiliser la langue du navigateur", "Enregistrer les préférences de visionnage"),
    "es": ("Idioma", "Usar idioma del navegador", "Guardar preferencias de visualización"),
    "de": ("Sprache", "Browsersprache verwenden", "Wiedergabeeinstellungen speichern"),
    "pt-BR": ("Idioma", "Usar o idioma do navegador", "Salvar preferências de visualização"),
    "ko": ("언어", "브라우저 언어 사용", "시청 환경설정 저장"),
    "ja": ("言語", "ブラウザの言語を使用", "視聴設定を保存")
}

FEED = {
    "en": (
        "Sign in to see your feed",
        "Manage your feed",
        "Unfollow {name}",
        "Sign in to manage",
        "Videos",
        "Appearances",
        "Likes",
        "Save activity preferences",
        "No public videos from your follows.",
        "No public videos from creators you follow.",
        "Feed pages",
        "Previous page",
        "Next page",
    ),
    "fr": (
        "Connectez-vous pour voir votre fil",
        "Gérer votre fil",
        "Ne plus suivre {name}",
        "Connectez-vous pour gérer",
        "Vidéos",
        "Apparitions",
        "J’aime",
        "Enregistrer les préférences d’activité",
        "Aucune vidéo publique de vos abonnements.",
        "Aucune vidéo publique des créateurs que vous suivez.",
        "Pages du fil",
        "Page précédente",
        "Page suivante",
    ),
    "es": (
        "Inicia sesión para ver tu feed",
        "Administra tu feed",
        "Dejar de seguir a {name}",
        "Inicia sesión para administrar",
        "Videos",
        "Apariciones",
        "Me gusta",
        "Guardar preferencias de actividad",
        "No hay videos públicos de las cuentas que sigues.",
        "No hay videos públicos de los creadores que sigues.",
        "Páginas del feed",
        "Página anterior",
        "Página siguiente",
    ),
    "de": (
        "Melde dich an, um deinen Feed zu sehen",
        "Deinen Feed verwalten",
        "{name} nicht mehr folgen",
        "Melde dich an, um zu verwalten",
        "Videos",
        "Auftritte",
        "Gefällt mir",
        "Aktivitätseinstellungen speichern",
        "Keine öffentlichen Videos von Konten, denen du folgst.",
        "Keine öffentlichen Videos von Creators, denen du folgst.",
        "Feed-Seiten",
        "Vorherige Seite",
        "Nächste Seite",
    ),
    "pt-BR": (
        "Entre para ver seu feed",
        "Gerencie seu feed",
        "Deixar de seguir {name}",
        "Entre para gerenciar",
        "Vídeos",
        "Aparições",
        "Curtidas",
        "Salvar preferências de atividade",
        "Nenhum vídeo público de quem você segue.",
        "Nenhum vídeo público dos criadores que você segue.",
        "Páginas do feed",
        "Página anterior",
        "Próxima página",
    ),
    "ko": (
        "피드를 보려면 로그인하세요",
        "피드 관리",
        "{name} 팔로우 취소",
        "관리하려면 로그인하세요",
        "동영상",
        "출연",
        "좋아요",
        "활동 환경설정 저장",
        "팔로우한 사용자의 공개 동영상이 없습니다.",
        "팔로우하는 크리에이터의 공개 동영상이 없습니다.",
        "피드 페이지",
        "이전 페이지",
        "다음 페이지",
    ),
    "ja": (
        "フィードを表示するにはログインしてください",
        "フィードを管理",
        "{name}のフォローを解除",
        "管理するにはログインしてください",
        "動画",
        "出演",
        "高評価",
        "アクティビティ設定を保存",
        "フォロー中のユーザーの公開動画はありません。",
        "フォローしているクリエイターの公開動画はありません。",
        "フィードページ",
        "前のページ",
        "次のページ",
    ),
}


FEEDBACK = {
    "Could not update your follows.": (
        "Could not update your follows.",
        "Impossible de mettre à jour vos abonnements.",
        "No se pudieron actualizar tus seguidos.",
        "Deine Follows konnten nicht aktualisiert werden.",
        "Não foi possível atualizar quem você segue.",
        "팔로우를 업데이트할 수 없습니다.",
        "フォローを更新できませんでした。"
    ),
    "Could not load your follow status.": (
        "Could not load your follow status.",
        "Impossible de charger votre statut d’abonnement.",
        "No se pudo cargar tu estado de seguimiento.",
        "Dein Follow-Status konnte nicht geladen werden.",
        "Não foi possível carregar seu status de seguidor.",
        "팔로우 상태를 불러올 수 없습니다.",
        "フォロー状態を読み込めませんでした。"
    ),
    "Could not unfollow creator.": (
        "Could not unfollow creator.",
        "Impossible de ne plus suivre le créateur.",
        "No se pudo dejar de seguir al creador.",
        "Der Creator konnte nicht entfolgt werden.",
        "Não foi possível deixar de seguir o criador.",
        "크리에이터 팔로우를 취소할 수 없습니다.",
        "クリエイターのフォローを解除できませんでした。"
    ),
    "Could not save preferences.": (
        "Could not save preferences.",
        "Impossible d’enregistrer les préférences.",
        "No se pudieron guardar las preferencias.",
        "Einstellungen konnten nicht gespeichert werden.",
        "Não foi possível salvar as preferências.",
        "환경설정을 저장할 수 없습니다.",
        "設定を保存できませんでした。"
    )
}
