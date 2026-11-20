"""Built-in registration labels and organizer-authored translations."""

from flask import abort, request

from library_interactivity import text_field


LANGUAGES = {
    "en": "English", "fr": "Français", "es": "Español", "de": "Deutsch", "pt-BR": "Português (Brasil)",
    "ko": "한국어", "ja": "日本語", "zh-Hans": "简体中文",
}
KEYS = (
    "language", "default_heading", "email", "first_name", "last_name", "choose_option", "privacy", "register",
    "return_heading", "return", "email_prompt", "code_prompt", "code", "verify", "other_email", "send", "back_to_form",
    "watch_time", "back", "continue", "step_one", "step_two", "field_details",
)
LABELS = {
    "en": (
        "Language", "Register to watch", "Email", "First name", "Last name", "Choose an option", "Privacy policy",
        "Register and watch", "Already registered", "Already registered?",
        "Enter the email you used to register to receive a one-time code.",
        "If that address has a registration, an eight-digit code is on its way.",
        "Registration code", "Open registration", "Use another email", "Send code", "Back to registration",
        "Share this viewing session's watch time with the event organizer (optional)",
        "Back", "Continue", "Step 1 of 2", "Step 2 of 2", "Read the terms",
    ),
    "fr": (
        "Langue", "Inscrivez-vous pour regarder", "Adresse e-mail", "Prénom", "Nom", "Choisissez une option",
        "Politique de confidentialité", "S’inscrire et regarder", "Déjà inscrit", "Déjà inscrit ?",
        "Saisissez l’adresse utilisée lors de l’inscription pour recevoir un code à usage unique.",
        "Si cette adresse est inscrite, un code à huit chiffres va lui être envoyé.",
        "Code d’inscription", "Ouvrir l’inscription", "Utiliser une autre adresse", "Envoyer le code",
        "Retour à l’inscription", "Partager la durée de visionnage avec l’organisateur (facultatif)",
        "Retour", "Continuer", "Étape 1 sur 2", "Étape 2 sur 2", "Lire les conditions",
    ),
    "es": (
        "Idioma", "Regístrate para ver", "Correo electrónico", "Nombre", "Apellidos", "Elige una opción",
        "Política de privacidad", "Registrarse y ver", "Ya registrado", "¿Ya te registraste?",
        "Introduce el correo que usaste al registrarte para recibir un código de un solo uso.",
        "Si esa dirección está registrada, recibirá un código de ocho dígitos.",
        "Código de registro", "Abrir registro", "Usar otro correo", "Enviar código", "Volver al registro",
        "Compartir el tiempo de visualización de esta sesión con el organizador (opcional)",
        "Atrás", "Continuar", "Paso 1 de 2", "Paso 2 de 2", "Leer las condiciones",
    ),
    "de": (
        "Sprache", "Registrieren, um das Video anzusehen", "E-Mail-Adresse", "Vorname", "Nachname",
        "Option auswählen", "Datenschutzerklärung", "Registrieren und ansehen", "Bereits registriert",
        "Schon registriert?", "Gib die E-Mail-Adresse deiner Registrierung ein, um einen Einmalcode zu erhalten.",
        "Wenn diese Adresse registriert ist, wird ein achtstelliger Code gesendet.",
        "Registrierungscode", "Registrierung öffnen", "Andere E-Mail-Adresse verwenden", "Code senden",
        "Zurück zur Registrierung", "Wiedergabedauer dieser Sitzung mit dem Veranstalter teilen (optional)",
        "Zurück", "Weiter", "Schritt 1 von 2", "Schritt 2 von 2", "Bedingungen lesen",
    ),
    "pt-BR": (
        "Idioma", "Cadastre-se para assistir", "E-mail", "Nome", "Sobrenome", "Selecione uma opção",
        "Política de privacidade", "Cadastrar-se e assistir", "Já cadastrado", "Já se cadastrou?",
        "Digite o e-mail usado no cadastro para receber um código de uso único.",
        "Se esse endereço estiver cadastrado, um código de oito dígitos será enviado.",
        "Código de cadastro", "Abrir cadastro", "Usar outro e-mail", "Enviar código", "Voltar ao cadastro",
        "Compartilhar o tempo de exibição desta sessão com o organizador (opcional)",
        "Voltar", "Continuar", "Etapa 1 de 2", "Etapa 2 de 2", "Leia os termos",
    ),
    "ko": (
        "언어", "시청하려면 등록하세요", "이메일", "이름", "성", "옵션을 선택하세요", "개인정보 처리방침",
        "등록하고 시청하기", "이미 등록됨", "이미 등록하셨나요?",
        "등록에 사용한 이메일을 입력하면 일회용 코드를 받을 수 있습니다.",
        "등록된 주소라면 8자리 코드가 전송됩니다.", "등록 코드", "등록 열기", "다른 이메일 사용",
        "코드 보내기", "등록으로 돌아가기", "이 세션의 시청 시간을 행사 주최자와 공유 (선택 사항)",
        "뒤로", "계속", "1/2단계", "2/2단계", "약관 읽기",
    ),
    "ja": (
        "言語", "登録して視聴する", "メールアドレス", "名", "姓", "選択してください", "プライバシーポリシー",
        "登録して視聴", "登録済み", "登録済みですか？", "登録したメールアドレスを入力してワンタイムコードを受け取ります。",
        "登録済みのアドレスには8桁のコードを送信します。", "登録コード", "登録を開く", "別のメールアドレスを使う",
        "コードを送信", "登録に戻る", "このセッションの視聴時間を主催者と共有する（任意）",
        "戻る", "続ける", "ステップ1/2", "ステップ2/2", "利用条件を読む",
    ),
    "zh-Hans": (
        "语言", "注册后观看", "电子邮箱", "名", "姓", "请选择", "隐私政策", "注册并观看",
        "已注册", "已经注册？", "请输入注册时使用的电子邮箱以接收一次性验证码。",
        "如果该地址已注册，将收到一个八位数验证码。", "注册验证码", "打开注册", "使用其他邮箱",
        "发送验证码", "返回注册", "与活动主办方共享本次观看时长（可选）", "返回", "继续",
        "第 1 步，共 2 步", "第 2 步，共 2 步", "阅读条款",
    ),
}


def validate(locales, fields):
    if not isinstance(locales, dict) or any(code not in LANGUAGES or code == "en" for code in locales):
        abort(422, "Choose supported registration languages.")
    supported = {field["id"]: field for field in fields if field["type"] != "hidden"}
    output = {}
    for code, source in locales.items():
        if not isinstance(source, dict):
            abort(422, "Enter valid registration translations.")
        headings = {name: text_field(source.get(name, ""), f"a translated {name}", maximum)
                    for name, maximum in (("heading", 80), ("description", 1000))}
        translated = source.get("fields", {})
        if not isinstance(translated, dict) or translated.keys() - supported.keys():
            abort(422, "Choose existing registration fields for translation.")
        entries = {}
        for key, values in translated.items():
            if not isinstance(values, dict):
                abort(422, "Enter valid field translations.")
            options = values.get("options", {})
            if not isinstance(options, dict) or options.keys() - set(supported[key]["options"]):
                abort(422, "Translate existing registration options.")
            entries[key] = dict(label=text_field(values.get("label", ""), "a translated label", 240),
                                options={name: text_field(value, "a translated option", 80)
                                         for name, value in options.items()})
        output[code] = dict(headings, fields=entries)
    return output


def choices(form, preview=None):
    translations = form.get("translations", {})
    catalogue = {}
    for code in LANGUAGES:
        if code != "en" and code not in translations:
            continue
        labels = dict(zip(KEYS, LABELS[code], strict=True))
        custom = translations.get(code, {})
        labels["heading"] = custom.get("heading") or (
            labels["default_heading"] if form["heading"] == LABELS["en"][1] else form["heading"])
        labels["description"] = custom.get("description") or form["description"]
        for field in form["fields"]:
            if field["type"] == "hidden":
                continue
            key = field["id"]
            localized = custom.get("fields", {}).get(key, {})
            labels[key] = localized.get("label") or field["label"]
            for index, value in enumerate(field["options"]):
                labels[f"{key}.option.{index}"] = localized.get("options", {}).get(value) or value
        catalogue[code] = labels
    selected = preview or request.form.get("registration_language") or request.args.get("lang")
    if selected not in catalogue:
        selected = "en"
        for language, quality in request.accept_languages:
            if quality > 0:
                match = next((code for code in catalogue if code.split("-")[0] == language.lower().split("-")[0]), None)
                if match:
                    selected = match
                    break
    return selected, catalogue
