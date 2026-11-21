"""Requisitos de las bases analizados con Gemini y comparados con la empresa (pedido del PO, 20/11/2026).

«Una vez que el agente saca la información, asume y, con lo que lee, hace el match con los datos que tiene la
empresa. Así no sale todo amarillo.» No se llama a Gemini de verdad: se simula su respuesta.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import config
from services import bases_facts, bases_reader, db, eligibility, llm, rag
from test_categorias_servicios_obras import PROFILE, TODAY, check, member, public_factory, tender_dict  # noqa: F401

SECTION = """[página 31]
3.5 REQUISITOS DE CALIFICACIÓN
A. EXPERIENCIA DEL POSTOR EN LA ESPECIALIDAD
El postor debe acreditar un monto facturado acumulado equivalente a S/ 1,250,000.00 (un millón doscientos
cincuenta mil con 00/100 soles), por la ejecución de obras similares, durante los diez (10) años anteriores.
B. PERSONAL CLAVE
Residente de obra: Ingeniero Civil colegiado y habilitado, con dos (2) años de experiencia como residente.
C. EQUIPAMIENTO ESTRATÉGICO
Un cargador frontal. Se acredita con copia de documentos que sustenten la propiedad, la posesión, el compromiso
de compra venta o alquiler.
D. Declaración jurada de cumplimiento de las especificaciones técnicas (Anexo N° 3)."""

CONTEXT = "[página 4] 1.4 CUANTÍA DE LA CONTRATACIÓN: La cuantía no se dará a conocer a los proveedores."


def answer(**extra):
    """Respuesta típica del modelo para SECTION y CONTEXT."""
    base = {
        "valor_estimado": {"monto": None, "moneda": None, "reservado": True, "pagina": 4},
        "consorcio": {"permitido": True, "max_integrantes": None, "participacion_minima": None},
        "prestaciones_accesorias": False,
        "requisitos": [
            {"tipo": "experiencia", "descripcion": "Facturación en obras similares", "monto_minimo": 1250000.00,
             "moneda": "PEN", "anios": 10, "pagina": 31, "cita": "monto facturado acumulado equivalente a S/ 1,250,000.00"},
            {"tipo": "personal", "descripcion": "Residente de obra", "cargo": "Residente de obra",
             "profesion": "Ingeniero Civil", "experiencia_meses": 24, "colegiado": True, "cantidad": 1,
             "momento": "oferta", "pagina": 31, "cita": "Residente de obra: Ingeniero Civil colegiado y habilitado"},
            {"tipo": "equipamiento", "descripcion": "Un cargador frontal", "admite_alquiler": True, "pagina": 31,
             "cita": "Un cargador frontal"},
            {"tipo": "documento", "descripcion": "Declaración jurada (Anexo N° 3)", "pagina": 31,
             "cita": "Declaración jurada de cumplimiento de las especificaciones técnicas"},
        ]}
    base.update(extra)
    return base


def facts_of(raw=None, section=SECTION, context=CONTEXT):
    return bases_facts.analyze(section, context, "obras", [31], generate=lambda prompt: raw or answer())


def with_facts(facts, **tender):
    return eligibility.use_facts(dict(tender_dict(**tender), ocid="o-7001", source_file="bases.pdf"), facts)


# --------------------------------------------------------------------------- lo que devuelve el modelo
def test_se_guardan_solo_los_numeros_que_estan_escritos_en_las_bases():
    raw = answer()
    raw["requisitos"][0]["monto_minimo"] = 1500000          # no está en las bases: se descarta
    raw["requisitos"][1]["experiencia_meses"] = 24          # «dos (2) años» = 24 meses: vale
    raw["valor_estimado"] = {"monto": 980000, "moneda": "PEN", "reservado": False, "pagina": 99}
    facts = facts_of(raw)
    assert facts["requisitos"][0]["monto_minimo"] is None
    assert facts["requisitos"][1]["experiencia_meses"] == 24
    assert facts["valor_estimado"]["monto"] is None and facts["valor_estimado"]["pagina"] is None


def test_lee_los_montos_con_separadores_de_miles():
    facts = facts_of()
    assert facts["requisitos"][0]["monto_minimo"] == 1250000.0 and facts["requisitos"][0]["anios"] == 10
    assert facts["valor_estimado"]["reservado"] is True
    assert bases_facts.numbers_in("S/ 1 250 000.00 y 320.960,50") >= {Decimal("1250000"), Decimal("320960.5")}


def test_descarta_tipos_desconocidos_y_respuestas_raras():
    facts = facts_of({"requisitos": [{"tipo": "inventado"}, "texto", {"tipo": "otro", "descripcion": "X"}]})
    assert [r["tipo"] for r in facts["requisitos"]] == ["otro"]
    assert facts_of(["no es un dict"])["requisitos"] == []


def test_el_texto_que_se_envia_lleva_las_paginas_y_las_otras_partes():
    section = SimpleNamespace(text="AAAA\nBBBB", page_marks=[(0, 7), (5, 8)])
    assert bases_facts.marked_section(section) == "[página 7]\nAAAA\n\n[página 8]\nBBBB"
    prompt = bases_facts.build_prompt("SECCION", "OTRAS", "obras")
    assert "SECCION" in prompt and "OTRAS" in prompt and "La convocatoria es de obras" in prompt


def test_toma_de_las_bases_la_cuantia_el_consorcio_y_el_personal_clave():
    text = ("CAPÍTULO I\n1.4 CUANTÍA DE LA CONTRATACIÓN\nLa cuantía no se dará a conocer.\n" + "x" * 2000 +
            "\nREQUISITOS DE CALIFICACIÓN … experiencia\n" + "y" * 2000 + "\n2.4 CONSORCIO: se permite hasta dos integrantes.")
    doc = bases_reader.DocumentText(name="bases.pdf", text=text, starts=[0, 1500, 3000])
    section_start = text.index("REQUISITOS")
    context = bases_reader.context_text(doc, skip=(section_start, section_start + 100))
    assert context.startswith("[página 1]") and "no se dará a conocer" in context
    assert "[página 3]" in context and "hasta dos integrantes" in context


# --------------------------------------------------------------------------- comparación con la empresa
def test_con_los_requisitos_analizados_la_ficha_no_sale_toda_amarilla():
    members = [dict(member(36), evidence={"kinds": ["cv", "colegiatura", "experiencia"], "mismatch": [], "expired": []})]
    profile = dict(PROFILE, similar_contracts_amount=Decimal("1300000"))
    result = eligibility.evaluate(with_facts(facts_of()), profile, [], {}, TODAY, members=members)
    states = {c["label"]: c["status"] for c in result["checks"] if c.get("text")}
    assert states == {"Facturación en obras similares": "pass", "Residente de obra": "pass",
                      "Un cargador frontal": "assumed", "Declaración jurada (Anexo N° 3)": "assumed"}
    assert check(result, "monto")["status"] == "info" and "reservado" in check(result, "monto")["reason"]
    assert result["color"] == "verde" and "revise lo que se dio por cumplido" in result["reason"]


def test_si_no_alcanza_la_experiencia_dice_cuanto_falta_y_que_un_socio_puede_sumar():
    result = eligibility.evaluate(with_facts(facts_of()), PROFILE, [], {}, TODAY, members=[])
    experience = next(c for c in result["checks"] if c["label"] == "Facturación en obras similares")
    assert experience["status"] == "short" and "S/ 900,000.00" in experience["reason"]
    assert "S/ 1,250,000.00" in experience["reason"] and "socio" in experience["reason"]
    assert result["color"] == "amarillo"


def test_sin_consorcio_la_experiencia_que_no_alcanza_es_rojo():
    facts = facts_of(answer(consorcio={"permitido": False}))
    result = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[])
    assert result["color"] == "rojo"


def test_experiencia_en_veces_el_valor_estimado():
    raw = answer(valor_estimado={"monto": 1000000, "moneda": "PEN", "reservado": False, "pagina": 4})
    raw["requisitos"] = [{"tipo": "experiencia", "descripcion": "Facturación", "veces_valor_estimado": 2,
                          "cita": "dos (2) veces el valor estimado"}]
    facts = facts_of(raw, section=SECTION + " dos (2) veces el valor estimado", context="Valor estimado S/ 1,000,000.00")
    result = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[])
    assert result["amount"] == Decimal("1000000") and result["amount_source"] == "indicado en las bases"
    assert "S/ 2,000,000.00 (2 veces el valor estimado)" in result["checks"][-1]["reason"]
    reserved = eligibility.evaluate(with_facts(facts_of(dict(raw, valor_estimado={"reservado": True}),
                                                        section=SECTION + " dos (2) veces")),
                                    PROFILE, [], {}, TODAY, members=[])
    assert reserved["checks"][-1]["status"] == "info"


def test_requisito_en_dolares_se_compara_con_el_tipo_de_cambio_de_referencia(monkeypatch):
    monkeypatch.setattr(config, "USD_RATE", 3.40)
    raw = answer()
    raw["requisitos"] = [{"tipo": "experiencia", "descripcion": "Facturación", "monto_minimo": 320960,
                          "moneda": "USD", "cita": "USD $ 320,960.00"}]
    facts = facts_of(raw, section="monto facturado acumulado equivalente a USD $ 320,960.00")
    result = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=[])
    reason = result["checks"][-1]["reason"]
    assert result["checks"][-1]["status"] == "short" and "S/ 1,091,264.00" in reason and "S/ 3.4 por dólar" in reason


def test_personal_que_falta_y_personal_que_se_pide_para_el_contrato():
    facts = facts_of()
    junior = [dict(member(12), evidence={"kinds": ["cv", "colegiatura"], "mismatch": [], "expired": []})]
    short = eligibility.evaluate(with_facts(facts), PROFILE, [], {}, TODAY, members=junior)
    resident = next(c for c in short["checks"] if c["label"] == "Residente de obra")
    assert resident["status"] == "short" and "1 año" in resident["reason"] and "2 años" in resident["reason"]
    raw = answer()
    raw["requisitos"][1]["momento"] = "contrato"
    later = eligibility.evaluate(with_facts(facts_of(raw)), PROFILE, [], {}, TODAY, members=junior)
    resident = next(c for c in later["checks"] if c["label"] == "Residente de obra")
    assert resident["status"] == "assumed" and "firmar el contrato" in resident["reason"]


def test_rnp_y_habilitaciones_se_comparan_con_mi_empresa_y_mis_documentos():
    raw = answer()
    raw["requisitos"] = [
        {"tipo": "rnp", "descripcion": "RNP de ejecutor de obras", "registro_rnp": "ejecutor_obras", "cita": "RNP"},
        {"tipo": "habilitacion", "descripcion": "Registro de Hidrocarburos", "documento": "Registro de Hidrocarburos de Osinergmin",
         "cita": "Registro de Hidrocarburos"}]
    facts = facts_of(raw)
    document = {"id": 1, "filename": "osinergmin_hidrocarburos.pdf", "document_type": "otro", "valid_from": None,
                "expires_on": TODAY + timedelta(days=90)}
    result = eligibility.evaluate(with_facts(facts), PROFILE, [document], {}, TODAY, members=[])
    states = [c["status"] for c in result["checks"] if c.get("text")]
    assert states == ["pass", "pass"]
    missing = eligibility.evaluate(with_facts(facts), dict(PROFILE, rnp_registers=["bienes"]), [], {}, TODAY, members=[])
    reasons = [c["reason"] for c in missing["checks"] if c.get("text")]
    assert "márquela en Mi empresa" in reasons[0] and "Suba en Mis documentos" in reasons[1]
    assert not any(c["key"] == "rnp_registro" for c in missing["checks"])       # no se repite


def test_la_respuesta_de_la_empresa_manda_sobre_lo_calculado():
    tender = with_facts(facts_of())
    key = tender["requirements"][0]["key"]
    result = eligibility.evaluate(tender, PROFILE, [], {"reviews": {key: "no"}}, TODAY, members=[])
    assert result["color"] == "rojo"
    result = eligibility.evaluate(tender, PROFILE, [], {"reviews": {key: "own"}}, TODAY, members=[])
    assert next(c for c in result["checks"] if c["key"] == key)["status"] == "pass"


def test_sin_monto_sugiere_preguntarlo_en_consultas_mientras_haya_plazo():
    tender = dict(tender_dict(category="servicios", reqs=()), enquiry_end=datetime(2026, 11, 25, 22, tzinfo=timezone.utc))
    reason = check(eligibility.evaluate(tender, PROFILE, [], {}, TODAY, members=[]), "monto")["reason"]
    assert "consultas y observaciones hasta el 25/11/2026" in reason
    closed = dict(tender, enquiry_end=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert "consultas" not in check(eligibility.evaluate(closed, PROFILE, [], {}, TODAY, members=[]), "monto")["reason"]


def test_sin_analisis_se_mantiene_la_lectura_anterior():
    tender = eligibility.use_facts(dict(tender_dict(), ocid="o", source_file="b.pdf"), {"requisitos": []})
    assert tender["requirements"][0]["key"] == "k0" and "fact" not in tender["requirements"][0]


# --------------------------------------------------------------------------- lectura y análisis
@pytest.fixture
def factory():
    engine = db.make_engine("sqlite://")
    db.create_tables(engine)
    yield db.make_session_factory(engine)
    engine.dispose()


def add(factory, code, chunks=("A. EXPERIENCIA S/ 1,250,000.00",), facts=None):
    with factory.begin() as session:
        row = db.Tender(ocid=f"o-{code}", code=code, title="X", state="vigente", category="obras",
                        requirements_status="extraido" if chunks else "pendiente", bases_facts=facts,
                        bases_facts_at=datetime.now(timezone.utc) if facts else None)
        row.documents.append(db.TenderDocument(title="Bases", url=f"https://seace.example/{code}.pdf", is_bases=True))
        for i, text in enumerate(chunks):
            row.chunks.append(db.RequirementChunk(chunk_index=i, text=text, page=31, source="texto",
                                                  file_name="bases.pdf", embedding=[.1] * 768))
        session.add(row)


def test_analiza_las_bases_ya_leidas_sin_volver_a_leerlas(factory):
    add(factory, "1"), add(factory, "2", facts={"requisitos": []}), add(factory, "3", chunks=())
    seen = []

    def fake(section, context, category, pages):
        seen.append((section, category, pages))
        return bases_facts.clean(answer(), section)
    summary = rag.analyze_saved(factory, analyze=fake, download=False)
    assert summary["analizado"] == 1 and seen == [("[página 31]\nA. EXPERIENCIA S/ 1,250,000.00", "obras", [31])]
    with factory() as session:
        stored = session.query(db.Tender).filter_by(code="1").one()
        assert stored.bases_facts["requisitos"][0]["monto_minimo"] == 1250000.0 and stored.bases_facts_at


def test_si_gemini_se_queda_sin_cuota_el_analisis_se_detiene(factory):
    add(factory, "1"), add(factory, "2")

    def broken(*args):
        raise llm.ModelError("El modelo respondió con el error 429", code=429)
    summary = rag.analyze_saved(factory, analyze=broken, download=False)
    assert summary["stopped"] and summary["analizado"] == 0


def test_al_leer_bases_nuevas_se_analizan_y_si_falla_se_guarda_la_lectura(factory, monkeypatch):
    add(factory, "1", chunks=())
    result = bases_reader.ReadResult("extraido", bases_reader.Section(text="A. EXPERIENCIA S/ 1,250,000.00",
                                     document="b.pdf", source="texto", page_marks=[(0, 31)]), context="cuantía")
    monkeypatch.setattr(bases_reader, "read_bases", lambda content, use_ocr=None: result)
    fetch = lambda url: SimpleNamespace(chunks=[b"%PDF"])
    embed = lambda texts, task: [[0.0] * 768 for _ in texts]
    calls = []

    def analyze(section, context, category, pages):
        calls.append((context, category, pages))
        return bases_facts.clean(answer(), section)
    prepared = rag.prepare_bases("https://x", fetch=fetch, embed=embed, analyze=analyze, category="obras")
    assert calls == [("cuantía", "obras", [31])] and prepared[3]["requisitos"]

    def broken(*args):
        raise llm.ModelError("sin cuota", code=429)
    assert rag.prepare_bases("https://x", fetch=fetch, embed=embed, analyze=broken)[3] is None
    with factory.begin() as session:
        tender = session.query(db.Tender).filter_by(code="1").one()
        rag.index_tender(session, tender, prepared=prepared)
        assert tender.requirements_status == "extraido" and tender.bases_facts["requisitos"]


def test_por_defecto_no_se_analiza_sin_clave(monkeypatch):
    monkeypatch.setattr(config, "BASES_ANALYSIS", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    assert not bases_facts.enabled()
    monkeypatch.setattr(config, "GEMINI_API_KEY", "clave")
    assert bases_facts.enabled()


def test_ingest_job_analizar_requisitos(tmp_path, monkeypatch, capsys):
    import ingest_job
    url = f"sqlite:///{tmp_path / 'base.db'}"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    assert ingest_job.main_with(["--analizar-requisitos"]) == 0
    assert "Análisis de requisitos omitido" in capsys.readouterr().out
    monkeypatch.setattr(config, "BASES_ANALYSIS", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "clave")
    seen = []
    monkeypatch.setattr(rag, "analyze_saved", lambda factory, limit=None, codes=None, progress=None:
                        seen.append((limit, codes)) or {"processed": 0, "analizado": 0, "error": 0, "requisitos": 0,
                                                        "stopped": None, "details": [], "remaining": 0})
    assert ingest_job.main_with(["--analizar-requisitos", "5", "--codigo", "1265987"]) == 0
    assert seen == [(5, ["1265987"])]


def test_la_ficha_muestra_cada_requisito_analizado(account_client, account_factory, public_factory):
    from test_categorias_servicios_obras import add_tender, signed_in
    add_tender(public_factory, "1010", "obras", "7214", "Licitación Pública Abreviada", chunks=(SECTION,))
    with public_factory.begin() as session:
        tender = session.query(db.Tender).filter_by(code="1010").one()
        bases_facts.stamp(tender, facts_of())
    signed_in(account_client, account_factory)
    html = account_client.get("/convocatorias/1010").get_data(as_text=True)
    assert "Facturación en obras similares" in html and "Se da por cumplido" in html
    assert "Un cargador frontal" in html and "reservado" in html
    assert 'name="review_' in html and "Sin responder" in html


def test_el_analisis_y_la_lectura_piden_razonamiento_minimo(monkeypatch):
    from google.genai import errors
    seen = []

    def fake(prompt, model, langsmith_extra=None, cheap=None, max_tokens=None):
        seen.append((cheap, max_tokens))
        if cheap and len(seen) == 1:          # un modelo que no admite «minimal»: se repite sin fijarlo
            raise errors.APIError(400, {"error": {"message": "thinking_level"}})
        return {"text": '{"requisitos": []}', "usage_metadata": {}}
    monkeypatch.setattr(llm, "_call_gemini", fake)
    bases_facts.analyze("x")
    assert seen == [(llm.LOW_COST, bases_facts.ANALYSIS_MAX_TOKENS), (None, bases_facts.ANALYSIS_MAX_TOKENS)]
    assert llm._thinking(llm.LOW_COST).thinking_level.value == "MINIMAL" and llm._thinking(None) is None



def test_sin_cuota_de_texto_no_se_analiza_al_leer_y_queda_para_despues(monkeypatch):
    result = bases_reader.ReadResult("extraido", bases_reader.Section(text="A", document="b.pdf", source="texto"))
    calls = []

    def out(*args):
        calls.append(1)
        raise llm.ModelError("429", code=429, detail="per day")
    assert rag._analyze_section(result, out) is None and llm.exhausted("texto")
    monkeypatch.setattr(config, "BASES_ANALYSIS", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "clave")
    assert rag._analyze_section(result) is None             # con la cuota agotada, ni se intenta


def test_la_ingesta_no_envia_trazas_a_langsmith(monkeypatch):
    import ingest_job
    import langsmith
    calls = []
    monkeypatch.setattr(langsmith, "configure", lambda **kw: calls.append(kw.get("enabled")))
    monkeypatch.setattr(ingest_job, "_main", lambda args=None: calls.append("ingesta") or 0)
    monkeypatch.delenv("INGESTA_TRAZAS", raising=False)
    assert ingest_job.main([]) == 0
    assert calls == [False, "ingesta", None]          # apagadas durante la ingesta y restauradas al final
    calls.clear()
    monkeypatch.setenv("INGESTA_TRAZAS", "si")
    ingest_job.main([])
    assert calls == ["ingesta"]



def test_un_limite_por_minuto_no_detiene_el_analisis(factory, monkeypatch, capsys):
    add(factory, "1"), add(factory, "2")
    monkeypatch.setattr(rag, "ANALYSIS_WAITS", (0,))
    calls = []

    def busy_once(section, context, category, pages):
        calls.append(1)
        if len(calls) == 1:
            raise llm.ModelError("El modelo respondió con el error 429", code=429,
                                 detail="quotaId: GenerateRequestsPerMinutePerProjectPerModel")
        return bases_facts.clean(answer(), section)
    summary = rag.analyze_saved(factory, analyze=busy_once, download=False)
    assert not summary["stopped"] and summary["analizado"] == 2
    assert "GenerateRequestsPerMinutePerProjectPerModel" in capsys.readouterr().out


def test_sin_cuota_del_dia_el_analisis_se_detiene_y_dice_que_limite(factory):
    add(factory, "1")

    def daily(*args):
        raise llm.ModelError("El modelo respondió con el error 429", code=429,
                             detail="Quota exceeded for metric: generate_content_requests_per_day")
    summary = rag.analyze_saved(factory, analyze=daily, download=False)
    assert summary["stopped"] and "generate_content_requests_per_day" in summary["stopped"]